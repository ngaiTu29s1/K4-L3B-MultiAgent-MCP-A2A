from __future__ import annotations

import json
import os
from typing import Any

import httpx2

PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}


def _fallback(
    case: dict[str, Any],
    entity: dict[str, Any],
    shipment: dict[str, Any],
    payment: dict[str, Any],
) -> dict[str, Any]:
    claims = case.get("customer_request", {}).get("claims", [])
    topics = [
        claim.get("topic")
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("topic"), str)
    ]
    primary = next((topic for topic in topics if topic in PRIMARY_ISSUES), None)
    if entity["output"]["status"] != "resolved":
        primary = "insufficient_evidence"
    elif primary is None:
        primary = {
            "seller_delay": "late_delivery_seller",
            "logistics_delay": "late_delivery_logistics",
            "duplicate_capture": "duplicate_charge",
            "capture_mismatch": "payment_mismatch",
            "refund_pending": "refund_pending",
            "refund_failed": "refund_failed",
        }.get(payment["output"]["verdict"])
        primary = primary or {
            "seller_delay": "late_delivery_seller",
            "logistics_delay": "late_delivery_logistics",
        }.get(shipment["output"]["verdict"], "insufficient_evidence")

    no_action = {"valid_split_payment", "unsupported_claim"}
    case_status = (
        "needs_investigation"
        if primary == "insufficient_evidence"
        else "no_action"
        if primary in no_action
        else "action_required"
    )
    seller_id = next(
        iter(shipment["output"]["late_seller_ids"] or shipment.get("seller_ids", [])), None
    )
    party = {
        "late_delivery_seller": {"party_type": "seller", "party_id": seller_id},
        "late_delivery_logistics": {
            "party_type": "logistics_provider",
            "party_id": None,
        },
        "payment_mismatch": {"party_type": "payment_provider", "party_id": None},
        "duplicate_charge": {"party_type": "payment_provider", "party_id": None},
        "refund_pending": {"party_type": "payment_provider", "party_id": None},
        "refund_failed": {"party_type": "payment_provider", "party_id": None},
        "canceled_order_paid": {"party_type": "platform", "party_id": None},
        "unavailable_order_paid": {"party_type": "platform", "party_id": None},
    }.get(primary, {"party_type": "unknown", "party_id": None})
    cause = primary.upper()

    shipment_refs = shipment.get("evidence_refs", [])
    payment_refs = payment.get("evidence_refs", [])
    all_refs = list(
        dict.fromkeys([*entity.get("evidence_refs", []), *shipment_refs, *payment_refs])
    )
    claim_assessments = []
    refundable = payment["output"].get("refundable_total_brl") or 0
    for claim in claims[:5]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        refs = (
            shipment_refs
            if topic in {"late_delivery_seller", "late_delivery_logistics"}
            else payment_refs
            if topic
            in {
                "valid_split_payment",
                "payment_mismatch",
                "duplicate_charge",
                "refund_pending",
                "refund_failed",
                "requested_full_refund",
                "canceled_order_paid",
                "unavailable_order_paid",
            }
            else all_refs
        )
        if topic == "unsupported_claim":
            verdict, confidence = "unsupported", 0.9
        elif topic == primary:
            verdict, confidence = "supported", 0.9
        elif topic == "requested_full_refund":
            verdict = (
                "partially_supported"
                if refundable and case_status == "action_required"
                else "unsupported"
            )
            confidence = 0.8
        else:
            verdict, confidence = "insufficient_evidence", 0.5
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": list(dict.fromkeys(refs)),
            }
        )

    actions = {
        "no_action": ["Close case with evidence summary"],
        "needs_investigation": ["Request additional authoritative evidence"],
    }.get(case_status, [f"Resolve {primary.replace('_', ' ')}"])
    return {
        "claim_assessments": claim_assessments,
        "primary_issue": primary,
        "secondary_issues": [topic for topic in topics if topic != primary][:10],
        "case_status": case_status,
        "confidence": 0.9 if all_refs and primary != "insufficient_evidence" else 0.5,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": cause, "rank": 1}],
            "responsible_parties": [party],
        },
        "resolution_actions": actions,
    }


class LLMCoordinator:
    def __init__(self) -> None:
        self.api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()

    async def run(
        self,
        case: dict[str, Any],
        entity: dict[str, Any],
        shipment: dict[str, Any],
        payment: dict[str, Any],
    ) -> dict[str, Any]:
        fallback = _fallback(case, entity, shipment, payment)
        if not self.api_key:
            return fallback

        context = {
            "customer_request": case.get("customer_request", {}),
            "entity_resolution": entity["output"],
            "shipment": shipment["output"],
            "shipment_seller_ids": shipment.get("seller_ids", []),
            "payment": payment["output"],
            "evidence_refs": {
                "entity": entity.get("evidence_refs", []),
                "shipment": shipment.get("evidence_refs", []),
                "payment": payment.get("evidence_refs", []),
            },
        }
        prompt = (
            "Return one JSON object only. Assess every supplied claim using only the evidence "
            "summary. Required keys: claim_assessments (claim_id, verdict, confidence, "
            "evidence_refs), primary_issue, secondary_issues, case_status, confidence, "
            "root_cause_analysis (ranked_causes and responsible_parties), resolution_actions. "
            f"primary_issue must be one of {sorted(PRIMARY_ISSUES)}. Context: "
            + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        )
        try:
            async with httpx2.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "temperature": 0,
                        "max_completion_tokens": 700,
                        "reasoning_effort": "low",
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a concise e-commerce case coordinator.",
                            },
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                result = json.loads(content)
            if not isinstance(result, dict) or result.get("primary_issue") not in PRIMARY_ISSUES:
                return fallback
            return {**fallback, **result}
        except Exception:
            return fallback
