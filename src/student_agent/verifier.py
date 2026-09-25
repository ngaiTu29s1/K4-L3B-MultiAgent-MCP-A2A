from __future__ import annotations

import re
from typing import Any

from .context import InvestigationContext
from .llm_coordinator import PRIMARY_ISSUES

CLAIM_VERDICTS = {
    "supported",
    "unsupported",
    "partially_supported",
    "insufficient_evidence",
}
PARTY_TYPES = {
    "seller",
    "platform",
    "logistics_provider",
    "payment_provider",
    "customer",
    "unknown",
}


def _confidence(value: Any, default: float = 0.5) -> float:
    if not isinstance(value, int | float):
        return default
    return max(0.0, min(1.0, float(value)))


def _unique_strings(value: Any, limit: int, length: int = 80) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item[:length] for item in value if isinstance(item, str) and item)
    )[:limit]


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _cause_code(value: Any, fallback: str) -> str:
    raw = value if isinstance(value, str) else fallback
    normalized = re.sub(r"[^A-Z0-9_]", "_", raw.upper()).strip("_")[:80]
    return normalized if len(normalized) >= 3 and normalized[0].isalpha() else fallback


class Verifier:
    def __init__(self, context: InvestigationContext) -> None:
        self.context = context

    def run(
        self,
        entity: dict[str, Any],
        shipment: dict[str, Any],
        payment: dict[str, Any],
        coordinator: dict[str, Any],
    ) -> dict[str, Any]:
        primary = coordinator.get("primary_issue")
        if primary not in PRIMARY_ISSUES:
            primary = "insufficient_evidence"
        case_status = coordinator.get("case_status")
        if case_status not in {"action_required", "no_action", "needs_investigation"}:
            case_status = "needs_investigation"

        secondary = [
            issue
            for issue in _unique_strings(coordinator.get("secondary_issues"), 10)
            if issue != primary
        ]
        evidence_refs = sorted(self.context.evidence_refs)[:30]
        evidence_set = set(evidence_refs)
        supplied_assessments = {
            item.get("claim_id"): item
            for item in _list(coordinator.get("claim_assessments"))
            if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
        }
        claim_assessments: list[dict[str, Any]] = []
        claims = self.context.case_data.get("customer_request", {}).get("claims", [])
        for claim in claims[:5]:
            if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
                continue
            item = supplied_assessments.get(claim["claim_id"], {})
            verdict = item.get("verdict")
            if verdict not in CLAIM_VERDICTS:
                verdict = "insufficient_evidence"
            refs = [
                ref
                for ref in _unique_strings(item.get("evidence_refs"), 30, 99)
                if ref in evidence_set
            ]
            claim_assessments.append(
                {
                    "claim_id": claim["claim_id"][:64],
                    "verdict": verdict,
                    "confidence": _confidence(item.get("confidence")),
                    "evidence_refs": refs,
                }
            )

        analysis = coordinator.get("root_cause_analysis")
        analysis = analysis if isinstance(analysis, dict) else {}
        causes = []
        for rank, item in enumerate(_list(analysis.get("ranked_causes"))[:5], 1):
            if isinstance(item, dict):
                causes.append(
                    {
                        "cause_code": _cause_code(item.get("cause_code"), primary.upper()),
                        "rank": rank,
                    }
                )
        if not causes:
            causes = [{"cause_code": primary.upper(), "rank": 1}]

        parties = []
        for item in _list(analysis.get("responsible_parties"))[:5]:
            if not isinstance(item, dict) or item.get("party_type") not in PARTY_TYPES:
                continue
            party_id = item.get("party_id")
            parties.append(
                {
                    "party_type": item["party_type"],
                    "party_id": party_id[:128] if isinstance(party_id, str) else None,
                }
            )
        if primary == "late_delivery_seller" and not any(
            party["party_type"] == "seller" for party in parties
        ):
            seller_id = next(
                iter(shipment["output"]["late_seller_ids"] or shipment.get("seller_ids", [])),
                None,
            )
            parties.append({"party_type": "seller", "party_id": seller_id})
        if primary == "late_delivery_logistics" and not any(
            party["party_type"] == "logistics_provider" for party in parties
        ):
            parties.append({"party_type": "logistics_provider", "party_id": None})
        if not parties:
            parties = [{"party_type": "unknown", "party_id": None}]
        parties = parties[:5]

        resolved = entity["output"]["resolved_order_ids"]
        refundable = payment["output"].get("refundable_total_brl")
        refundable = float(refundable) if isinstance(refundable, int | float) else 0.0
        requested_refund = any(
            isinstance(claim, dict) and claim.get("topic") == "requested_full_refund"
            for claim in claims
        )
        recommended = (
            round(refundable, 2)
            if requested_refund and case_status == "action_required"
            else 0.0
        )
        recommended = min(recommended, refundable)
        refund_lines = (
            [
                {
                    "reason_code": primary.upper(),
                    "amount_brl": recommended,
                    "entity_id": resolved[0] if resolved else None,
                }
            ]
            if recommended > 0
            else []
        )

        actions = _unique_strings(coordinator.get("resolution_actions"), 8)
        if not actions:
            actions = ["Request additional authoritative evidence"]
        related = sorted(set(entity.get("related_order_ids", [])) | set(resolved))[:20]
        output = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": self.context.case_id,
            "assessment": {
                "primary_issue": primary,
                "secondary_issues": secondary,
                "case_status": case_status,
                "confidence": _confidence(coordinator.get("confidence")),
            },
            "affected_entities": {
                "order_ids": resolved[:20],
                "item_ids": shipment.get("item_ids", [])[:20],
                "seller_ids": shipment.get("seller_ids", [])[:20],
                "payment_references": payment.get("payment_references", [])[:20],
                "shipment_ids": shipment.get("shipment_ids", [])[:20],
            },
            "claim_assessments": claim_assessments,
            "entity_resolution": entity["output"],
            "customer_context": {
                "customer_unique_id": entity.get("customer_unique_id"),
                "related_order_ids": related,
            },
            "shipment_analysis": shipment["output"],
            "payment_analysis": payment["output"],
            "root_cause_analysis": {
                "ranked_causes": causes,
                "responsible_parties": parties,
            },
            "evidence_refs": evidence_refs,
            "data_conflicts": shipment.get("conflicts", [])[:5],
            "financial_resolution": {
                "currency": "BRL",
                "recommended_refund_brl": recommended,
                "refund_lines": refund_lines,
            },
            "resolution_actions": actions,
        }
        self.context.trace.emit(
            case_id=self.context.case_id,
            event_type="verification_completed",
            actor="verifier",
            target="coordinator",
            decision_code="CONSISTENCY_CHECKED",
            evidence_refs=evidence_refs[:20],
        )
        return output
