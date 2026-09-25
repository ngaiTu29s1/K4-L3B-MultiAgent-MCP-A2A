from __future__ import annotations

import asyncio
from typing import Any

from .entity_customer import CaseEvidenceCache, resolve_entity_customer
from .mcp_gateway import EvidenceGateway
from .specialists import (
    investigate_order_product,
    investigate_payment_refund,
    investigate_shipment,
)
from .trace import TraceWriter


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _claim_topics(case: dict[str, Any]) -> list[str]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    return [
        claim["topic"]
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("topic"), str)
    ]


def _primary_issue(
    topics: list[str], order_products: list[Any], shipments: list[Any], payments: list[Any]
) -> str:
    shipment_verdicts = {result.verdict for result in shipments}
    payment_verdicts = {result.verdict for result in payments}
    order_statuses = {result.order_status for result in order_products}

    topic_map = {
        "late_delivery_seller": "late_delivery_seller",
        "late_delivery_logistics": "late_delivery_logistics",
        "valid_split_payment": "valid_split_payment",
        "canceled_order_paid": "canceled_order_paid",
        "unavailable_order_paid": "unavailable_order_paid",
        "payment_mismatch": "payment_mismatch",
        "duplicate_charge": "duplicate_charge",
        "refund_pending": "refund_pending",
        "refund_failed": "refund_failed",
    }
    for topic in topics:
        mapped = topic_map.get(topic)
        if mapped == "late_delivery_seller" and "seller_delay" in shipment_verdicts:
            return mapped
        if mapped == "late_delivery_logistics" and "logistics_delay" in shipment_verdicts:
            return mapped
        if mapped == "valid_split_payment" and "reconciled" in payment_verdicts:
            return mapped
        if mapped == "canceled_order_paid" and "canceled" in order_statuses:
            return mapped
        if mapped == "unavailable_order_paid" and "unavailable" in order_statuses:
            return mapped
        if mapped == "payment_mismatch" and "capture_mismatch" in payment_verdicts:
            return mapped
        if mapped == "duplicate_charge" and "duplicate_capture" in payment_verdicts:
            return mapped
        if mapped == "refund_pending" and "refund_pending" in payment_verdicts:
            return mapped
        if mapped == "refund_failed" and "refund_failed" in payment_verdicts:
            return mapped

    if "seller_delay" in shipment_verdicts:
        return "late_delivery_seller"
    if "logistics_delay" in shipment_verdicts:
        return "late_delivery_logistics"
    if "capture_mismatch" in payment_verdicts:
        return "payment_mismatch"
    if "duplicate_capture" in payment_verdicts:
        return "duplicate_charge"
    if "refund_pending" in payment_verdicts:
        return "refund_pending"
    if "refund_failed" in payment_verdicts:
        return "refund_failed"
    if "canceled" in order_statuses:
        return "canceled_order_paid"
    if "unavailable" in order_statuses:
        return "unavailable_order_paid"
    if "unsupported_claim" in topics:
        return "unsupported_claim"
    return "insufficient_evidence"


def _claim_verdict(topic: str, issue: str, evidence_available: bool) -> str:
    if topic == "requested_full_refund":
        return "partially_supported" if evidence_available else "insufficient_evidence"
    if topic == issue:
        return "supported"
    if topic in {"late_delivery_seller", "late_delivery_logistics"} and issue.startswith(
        "late_delivery_"
    ):
        return "partially_supported"
    return "unsupported" if evidence_available else "insufficient_evidence"


def _root_cause(issue: str, shipments: list[Any], order_products: list[Any]) -> dict[str, Any]:
    if issue == "late_delivery_seller":
        sellers = _unique([seller for result in shipments for seller in result.late_seller_ids])
        return {
            "ranked_causes": [{"cause_code": "SELLER_HANDOFF_DELAY", "rank": 1}],
            "responsible_parties": [
                {"party_type": "seller", "party_id": seller} for seller in sellers
            ],
        }
    if issue == "late_delivery_logistics":
        return {
            "ranked_causes": [{"cause_code": "LOGISTICS_DELIVERY_DELAY", "rank": 1}],
            "responsible_parties": [
                {"party_type": "logistics_provider", "party_id": None}
            ],
        }
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        cause = "CANCELED_ORDER_PAYMENT" if issue == "canceled_order_paid" else "UNAVAILABLE_ORDER_PAYMENT"
        return {
            "ranked_causes": [{"cause_code": cause, "rank": 1}],
            "responsible_parties": [{"party_type": "platform", "party_id": None}],
        }
    if issue in {"payment_mismatch", "duplicate_charge", "valid_split_payment"}:
        return {
            "ranked_causes": [{"cause_code": "PAYMENT_RECONCILIATION", "rank": 1}],
            "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
        }
    if issue in {"refund_pending", "refund_failed"}:
        return {
            "ranked_causes": [{"cause_code": "REFUND_PROCESSING", "rank": 1}],
            "responsible_parties": [{"party_type": "payment_provider", "party_id": None}],
        }
    return {
        "ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}],
        "responsible_parties": [{"party_type": "unknown", "party_id": None}],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    if not isinstance(request, dict):
        request = {}
    candidates = case.get("candidate_order_ids", [])
    if not isinstance(candidates, list):
        candidates = []
    claimed_order_id = request.get("claimed_order_id")
    if isinstance(claimed_order_id, str) and claimed_order_id not in candidates:
        candidates.insert(0, claimed_order_id)
    customer_hint = case.get("customer_unique_id_hint")
    customer_id = customer_hint if isinstance(customer_hint, str) else None
    cache = CaseEvidenceCache(case_id)

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-customer-agent",
        attributes={"candidate_count": len(candidates)},
    )
    entity = await resolve_entity_customer(
        case_id=case_id,
        candidate_order_ids=candidates,
        customer_unique_id=customer_id,
        gateway=gateway,
        trace=trace,
        evidence_cache=cache,
    )
    resolved_order_ids = list(entity.resolved_order_ids)
    rejected_candidates = _unique(
        list(entity.rejected_candidates)
        + ([candidate for candidate in candidates if candidate not in resolved_order_ids]
           if entity.status == "resolved" else [])
    )

    order_products: list[Any] = []
    shipments: list[Any] = []
    payments: list[Any] = []
    for order_id in resolved_order_ids:
        order_product, shipment, payment = await asyncio.gather(
            investigate_order_product(
                case_id=case_id,
                order_id=order_id,
                gateway=gateway,
                trace=trace,
                evidence_cache=cache,
            ),
            investigate_shipment(
                case_id=case_id,
                order_id=order_id,
                gateway=gateway,
                trace=trace,
                evidence_cache=cache,
            ),
            investigate_payment_refund(
                case_id=case_id,
                order_id=order_id,
                gateway=gateway,
                trace=trace,
                evidence_cache=cache,
            ),
        )
        order_products.append(order_product)
        shipments.append(shipment)
        payments.append(payment)

    policy_refs: list[str] = []
    try:
        policy = await cache.call(
            gateway,
            "get_policy",
            case_id=case_id,
            policy_version=str(case.get("policy_version", "")),
        )
        policy_refs.append(policy["evidence_ref"])
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor="coordinator",
            tool_name="get_policy",
            evidence_refs=policy_refs,
        )
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="coordinator",
            decision_code="POLICY_EVIDENCE_CONSUMED",
            evidence_refs=policy_refs,
        )
    except RuntimeError:
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="coordinator",
            decision_code="POLICY_UNAVAILABLE",
        )

    topics = _claim_topics(case)
    issue = _primary_issue(topics, order_products, shipments, payments)
    all_specialist_refs = [
        reference
        for result in [*order_products, *shipments, *payments]
        for reference in result.evidence_refs
    ]
    evidence_refs = _unique(list(entity.evidence_refs) + all_specialist_refs + policy_refs)[:30]
    evidence_available = bool(evidence_refs)
    claim_items = request.get("claims", []) if isinstance(request.get("claims"), list) else []
    claim_assessments = [
        {
            "claim_id": claim["claim_id"],
            "verdict": _claim_verdict(claim.get("topic", ""), issue, evidence_available),
            "confidence": 0.9 if claim.get("topic") == issue else 0.55,
            "evidence_refs": evidence_refs[: min(5, len(evidence_refs))],
        }
        for claim in claim_items
        if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
    ][:5]

    payment_refundable = [result.refundable_total_brl for result in payments if result.refundable_total_brl is not None]
    refundable_total = max(payment_refundable, default=0.0)
    recommended_refund = refundable_total if "requested_full_refund" in topics else 0.0
    if issue in {"unsupported_claim", "insufficient_evidence"}:
        recommended_refund = 0.0
    refund_lines = (
        [{"reason_code": issue.upper(), "amount_brl": recommended_refund, "entity_id": resolved_order_ids[0]}]
        if recommended_refund > 0 and resolved_order_ids
        else []
    )
    secondary_issues = _unique([topic for topic in topics if topic != issue])[:10]
    if issue in {"unsupported_claim", "insufficient_evidence"}:
        case_status = "needs_investigation"
    elif issue in {"valid_split_payment"} or issue == "unsupported_claim":
        case_status = "no_action"
    else:
        case_status = "action_required"
    all_sellers = _unique([seller for result in order_products for seller in result.seller_ids])
    all_items = _unique([item for result in order_products for item in result.item_ids])
    all_products = _unique([product for result in order_products for product in result.product_ids])
    all_shipments = _unique([shipment for result in shipments for shipment in result.shipment_ids])
    all_payment_refs = _unique([reference for result in payments for reference in result.payment_references])
    conflicts = [
        conflict
        for result in shipments
        for conflict in result.data_conflicts
    ][:5]
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": secondary_issues,
            "case_status": case_status,
            "confidence": 0.9 if entity.status == "resolved" and evidence_available else 0.2,
        },
        "affected_entities": {
            "order_ids": resolved_order_ids[:20],
            "item_ids": all_items[:20],
            "seller_ids": all_sellers[:20],
            "payment_references": all_payment_refs[:20],
            "shipment_ids": all_shipments[:20],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity.status,
            "resolved_order_ids": resolved_order_ids[:20],
            "rejected_candidates": rejected_candidates[:20],
            "confidence": entity.confidence,
        },
        "customer_context": {
            "customer_unique_id": entity.customer_unique_id,
            "related_order_ids": list(entity.related_order_ids)[:20],
        },
        "shipment_analysis": {
            "verdict": shipments[0].verdict if shipments else "insufficient_evidence",
            "late_seller_ids": _unique([seller for result in shipments for seller in result.late_seller_ids])[:20],
            "timeline_complete": bool(shipments) and all(result.timeline_complete for result in shipments),
        },
        "payment_analysis": {
            "verdict": payments[0].verdict if payments else "insufficient_evidence",
            "captured_total_brl": payments[0].captured_total_brl if payments else None,
            "refunded_total_brl": payments[0].refunded_total_brl if payments else None,
            "refundable_total_brl": payments[0].refundable_total_brl if payments else None,
        },
        "root_cause_analysis": _root_cause(issue, shipments, order_products),
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": (
            ["Process eligible refund"]
            if recommended_refund > 0
            else ["Close investigation with recorded evidence"]
            if issue == "unsupported_claim"
            else ["Investigate missing authoritative evidence"]
            if issue == "insufficient_evidence"
            else ["Review case and apply policy resolution"]
        ),
    }
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="OUTPUT_ASSEMBLED",
        evidence_refs=evidence_refs[:20],
    )
    return output
