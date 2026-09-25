from __future__ import annotations

from typing import Any

from .context import InvestigationContext
from .entity_customer import CaseEvidenceCache, resolve_entity_customer
from .llm_coordinator import LLMCoordinator
from .mcp_gateway import EvidenceGateway
from .specialists import (
    OrderProductHandoff,
    PaymentRefundHandoff,
    ShipmentHandoff,
    investigate_order_product,
    investigate_payment_refund,
    investigate_shipment,
)
from .trace import TraceWriter
from .verifier import Verifier


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    cache = CaseEvidenceCache(case_id)
    context = InvestigationContext(case_id, case, gateway, trace)

    # 1. Ban 1: Entity & Customer resolution
    entity_handoff = await resolve_entity_customer(
        case_id=case_id,
        candidate_order_ids=case.get("candidate_order_ids", []),
        customer_unique_id=case.get("customer_unique_id_hint"),
        gateway=gateway,
        trace=trace,
        evidence_cache=cache,
    )
    resolved_ids = entity_handoff.resolved_order_ids
    candidates = case.get("candidate_order_ids", [])
    rejected_candidates = [c for c in candidates if c not in resolved_ids] or list(
        entity_handoff.rejected_candidates
    )
    entity_dict = {
        "output": {
            "status": entity_handoff.status,
            "resolved_order_ids": list(resolved_ids),
            "rejected_candidates": list(dict.fromkeys(rejected_candidates)),
            "confidence": entity_handoff.confidence,
        },
        "customer_unique_id": entity_handoff.customer_unique_id,
        "related_order_ids": list(entity_handoff.related_order_ids),
        "evidence_refs": list(entity_handoff.evidence_refs),
    }

    # Determine primary order for specialists
    primary_order_id = (
        resolved_ids[0]
        if resolved_ids
        else next(
            (c for c in case.get("candidate_order_ids", []) if not c.startswith("candidate-")), ""
        )
    )

    # 2. Ban 2: Order/Product, Shipment, and Payment specialists
    if primary_order_id:
        order_product_handoff = await investigate_order_product(
            case_id=case_id,
            order_id=primary_order_id,
            gateway=gateway,
            trace=trace,
            evidence_cache=cache,
        )
        shipment_handoff = await investigate_shipment(
            case_id=case_id,
            order_id=primary_order_id,
            gateway=gateway,
            trace=trace,
            evidence_cache=cache,
        )
        payment_handoff = await investigate_payment_refund(
            case_id=case_id,
            order_id=primary_order_id,
            gateway=gateway,
            trace=trace,
            evidence_cache=cache,
        )
    else:
        order_product_handoff = OrderProductHandoff(
            case_id=case_id,
            order_id="",
            status="failed",
            order_status=None,
            item_ids=(),
            seller_ids=(),
            product_ids=(),
            product_categories=(),
            canceled=False,
            unavailable=False,
            confidence=0.0,
            evidence_refs=(),
        )
        shipment_handoff = ShipmentHandoff(
            case_id=case_id,
            order_id="",
            status="failed",
            verdict="insufficient_evidence",
            shipment_ids=(),
            late_seller_ids=(),
            timeline_complete=False,
            data_conflicts=(),
            confidence=0.0,
            evidence_refs=(),
        )
        payment_handoff = PaymentRefundHandoff(
            case_id=case_id,
            order_id="",
            status="failed",
            verdict="insufficient_evidence",
            captured_total_brl=None,
            refunded_total_brl=None,
            refundable_total_brl=None,
            payment_types=(),
            payment_installments=(),
            duplicate_capture=False,
            capture_mismatch=False,
            confidence=0.0,
            evidence_refs=(),
        )

    shipment_dict = {
        "output": {
            "verdict": shipment_handoff.verdict,
            "late_seller_ids": list(shipment_handoff.late_seller_ids),
            "timeline_complete": shipment_handoff.timeline_complete,
        },
        "seller_ids": list(order_product_handoff.seller_ids),
        "item_ids": list(order_product_handoff.item_ids),
        "shipment_ids": list(shipment_handoff.shipment_ids),
        "conflicts": list(shipment_handoff.data_conflicts),
        "evidence_refs": list(shipment_handoff.evidence_refs),
    }

    payment_dict = {
        "output": {
            "verdict": payment_handoff.verdict,
            "captured_total_brl": payment_handoff.captured_total_brl,
            "refunded_total_brl": payment_handoff.refunded_total_brl,
            "refundable_total_brl": payment_handoff.refundable_total_brl,
        },
        "payment_references": [primary_order_id] if primary_order_id else [],
        "evidence_refs": list(payment_handoff.evidence_refs),
    }

    # Synchronize all collected evidence references into context
    for response in cache._responses.values():
        if isinstance(response, dict) and "evidence_ref" in response:
            context.evidence_refs.add(response["evidence_ref"])

    # 3. Ban 3: Coordinator reasoning
    decision = await LLMCoordinator().run(case, entity_dict, shipment_dict, payment_dict)

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="VERIFY_DECISION",
    )

    # 4. Ban 3: Verifier & Consistency enforcement
    return Verifier(context).run(entity_dict, shipment_dict, payment_dict, decision)
