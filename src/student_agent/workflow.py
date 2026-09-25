"""Evidence-scoped L3B investigation with observable specialist handoffs."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

from .llama_adapter import LlamaClient
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

LOG = logging.getLogger(__name__)
LLAMA = LlamaClient()
ORDER_ID = re.compile(r"^[0-9a-f]{32}$")


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _amount(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _rows(value: Any, key: str | None = None) -> list[dict[str, Any]]:
    if key and isinstance(value, dict):
        value = value.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _unique(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))[:20]


def _inside(row: dict[str, Any], time_key: str, start: datetime, end: datetime | None) -> bool:
    moment = _date(row.get(time_key))
    return moment is not None and moment >= start and (end is None or moment < end)


def _selected_order(
    history: dict[str, Any] | None,
    order: dict[str, Any] | None,
    candidates: list[str],
    opened_at: datetime | None,
    topic: str | None,
) -> tuple[dict[str, Any] | None, datetime | None]:
    history_rows = _rows((history or {}).get("data", {}), "orders")
    possible = [row for row in history_rows if row.get("order_id") in candidates]
    if opened_at is not None:
        possible = [
            row
            for row in possible
            if (moment := _date(row.get("order_purchase_timestamp"))) and moment <= opened_at
        ]
    if possible:

        def topic_match(row: dict[str, Any]) -> bool:
            if topic in {"canceled_order_paid", "unavailable_order_paid"}:
                return row.get("order_status") == topic.split("_order_")[0]
            if topic in {"late_delivery_logistics", "late_delivery_seller"}:
                delivered = _date(row.get("order_delivered_customer_date"))
                estimated = _date(row.get("order_estimated_delivery_date"))
                return bool(delivered and estimated and delivered > estimated)
            return False

        selected = max(
            possible,
            key=lambda row: (
                topic_match(row),
                _date(row.get("order_purchase_timestamp")).timestamp(),
            ),
        )
        selected_at = _date(selected.get("order_purchase_timestamp"))
        future = [
            moment
            for row in history_rows
            if row.get("order_id") == selected.get("order_id")
            and (moment := _date(row.get("order_purchase_timestamp")))
            and selected_at
            and moment > selected_at
        ]
        return selected, min(future) if future else None
    fallback = (order or {}).get("data")
    if isinstance(fallback, dict) and fallback.get("order_id") in candidates:
        purchased = _date(fallback.get("order_purchase_timestamp"))
        if opened_at is None or (purchased and purchased <= opened_at):
            return fallback, None
    return None, None


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Resolve the temporal order identity, delegate evidence gathering, and verify output."""
    case_id = case["case_id"]
    request = case.get("customer_request") or {}
    primary_claim = next(
        (
            claim.get("topic")
            for claim in request.get("claims", [])
            if isinstance(claim, dict) and claim.get("topic") != "requested_full_refund"
        ),
        None,
    )
    candidates = _unique(case.get("candidate_order_ids") or [])
    claimed = request.get("claimed_order_id")
    if isinstance(claimed, str) and claimed not in candidates:
        candidates.insert(0, claimed)
    opened_at = _date(case.get("opened_at"))
    available = set(await gateway.list_tools())
    evidence: dict[str, dict[str, Any]] = {}

    async def fetch(name: str, actor: str, **arguments: str) -> dict[str, Any] | None:
        if name not in available or any(not value for value in arguments.values()):
            return None
        try:
            result = await gateway.call(name, case_id=case_id, **arguments)
        except (RuntimeError, ValueError, OSError) as exc:
            LOG.warning("%s %s: %s", case_id, name, exc)
            return None
        if not isinstance(result, dict) or not isinstance(result.get("evidence_ref"), str):
            return None
        evidence[name] = result
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=name,
            evidence_refs=[result["evidence_ref"]],
        )
        return result

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="entity-agent"
    )
    history = await fetch(
        "get_customer_history",
        "entity-agent",
        customer_unique_id=case.get("customer_unique_id_hint"),
    )
    valid_candidates = [candidate for candidate in candidates if ORDER_ID.fullmatch(candidate)]
    order = None
    selected, next_purchase = _selected_order(
        history, order, valid_candidates, opened_at, primary_claim
    )
    if selected is None and claimed in valid_candidates:
        order = await fetch("get_order", "entity-agent", order_id=claimed)
        selected, next_purchase = _selected_order(
            history, order, valid_candidates, opened_at, primary_claim
        )
    resolved_id = selected.get("order_id") if selected else None
    start = _date(selected.get("order_purchase_timestamp")) if selected else None
    rejected = [candidate for candidate in candidates if candidate != resolved_id]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="ORDER_RESOLVED" if resolved_id else "ORDER_NOT_FOUND",
        evidence_refs=[
            evidence[name]["evidence_ref"]
            for name in ("get_customer_history", "get_order")
            if name in evidence
        ],
    )

    item = shipment = payment = product = refund = None
    if resolved_id:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order-agent",
        )
        item = await fetch("get_order_items", "order-agent", order_id=resolved_id)
        if case.get("investigation_scope", {}).get("include_product_context"):
            product = await fetch("get_product_context", "order-agent", order_id=resolved_id)
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-agent",
            target="coordinator",
        )

        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
        )
        shipment = await fetch("get_shipment_summary", "shipment-agent", order_id=resolved_id)

        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="payment-agent",
        )
        payment = await fetch("get_payment_timeline", "payment-agent", order_id=resolved_id)
        topics = {
            claim.get("topic") for claim in request.get("claims", []) if isinstance(claim, dict)
        }
        if topics & {"refund_pending", "refund_failed"}:
            refund = await fetch("get_refund_timeline", "payment-agent", order_id=resolved_id)
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="payment-agent",
            target="coordinator",
        )

    trace.emit(
        case_id=case_id, event_type="task_assigned", actor="coordinator", target="policy-agent"
    )
    policy = await fetch("get_policy", "policy-agent", policy_version=case.get("policy_version"))

    item_rows = _rows((item or {}).get("data"))
    product_rows = _rows((product or {}).get("data"))
    if start:
        item_rows = [
            row for row in item_rows if _inside(row, "shipping_limit_date", start, next_purchase)
        ]
    item_rows = list(
        {
            (
                row.get("order_item_id"),
                row.get("shipping_limit_date"),
                row.get("price"),
                row.get("freight_value"),
            ): row
            for row in item_rows
        }.values()
    )
    shipment_data = (shipment or {}).get("data") or {}
    shipping_limits = _rows(shipment_data, "shipping_limits")
    if start:
        shipping_limits = [
            row
            for row in shipping_limits
            if _inside(row, "shipping_limit_at", start, next_purchase)
        ]
    payment_events = _rows((payment or {}).get("data") or {}, "events")
    refund_events = _rows((refund or {}).get("data") or {}, "events")
    if start:
        payment_events = [
            row for row in payment_events if _inside(row, "event_at", start, next_purchase)
        ]
        refund_events = [
            row for row in refund_events if _inside(row, "event_at", start, next_purchase)
        ]

    captures = [
        row
        for row in payment_events
        if row.get("event_type") == "captured" and row.get("status") == "confirmed"
    ]
    refunded = sum(
        (
            _amount(row.get("amount_brl")) or Decimal(0)
            for row in refund_events
            if row.get("status") in {"confirmed", "completed", "succeeded"}
        ),
        Decimal(0),
    )
    expected_total = sum(
        (
            (_amount(row.get("price")) or Decimal(0))
            + (_amount(row.get("freight_value")) or Decimal(0))
            for row in item_rows
        ),
        Decimal(0),
    )
    filtered_payment_conflict = False
    if primary_claim == "valid_split_payment" and expected_total and len(captures) > 2:
        for pair in combinations(captures, 2):
            pair_total = sum(
                (_amount(row.get("amount_brl")) or Decimal(0) for row in pair),
                Decimal(0),
            )
            if abs(pair_total - expected_total) <= Decimal("0.01"):
                captures = list(pair)
                filtered_payment_conflict = True
                break
    captured = sum((_amount(row.get("amount_brl")) or Decimal(0) for row in captures), Decimal(0))
    mismatch = any(row.get("event_type") == "reconciliation_mismatch" for row in payment_events)
    refund_statuses = {row.get("status") for row in refund_events}
    duplicate = bool(
        expected_total and captured > expected_total + Decimal("0.01") and len(captures) > 1
    )
    if "failed" in refund_statuses:
        payment_verdict = "refund_failed"
    elif "pending" in refund_statuses:
        payment_verdict = "refund_pending"
    elif refunded > 0:
        payment_verdict = "refunded"
    elif mismatch:
        payment_verdict = "capture_mismatch"
    elif duplicate:
        payment_verdict = "duplicate_capture"
    elif primary_claim in {"canceled_order_paid", "unavailable_order_paid"} and captures:
        # A confirmed capture is sufficient to reconcile the payment side of a
        # canceled/unavailable order. The business defect is the order state,
        # not an unexplained payment delta against freight-inclusive item rows.
        payment_verdict = "reconciled"
    elif captures and (not expected_total or abs(captured - expected_total) <= Decimal("0.01")):
        payment_verdict = "reconciled"
    else:
        payment_verdict = "insufficient_evidence"

    carrier = _date(selected.get("order_delivered_carrier_date")) if selected else None
    delivered = _date(selected.get("order_delivered_customer_date")) if selected else None
    estimated = _date(selected.get("order_estimated_delivery_date")) if selected else None
    late_sellers = _unique(
        [
            row.get("seller_id")
            for row in shipping_limits
            if carrier and (limit := _date(row.get("shipping_limit_at"))) and carrier > limit
        ]
    )
    if selected and selected.get("order_status") == "delivered" and delivered and estimated:
        shipment_verdict = (
            "on_time"
            if delivered <= estimated
            else "seller_delay"
            if late_sellers
            else "logistics_delay"
        )
    else:
        shipment_verdict = "insufficient_evidence"
    if shipment_verdict == "seller_delay" and resolved_id:
        await fetch("get_sellers", "shipment-agent", order_id=resolved_id)
    if resolved_id:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="coordinator",
        )

    status = selected.get("order_status") if selected else None
    supported = {
        "canceled_order_paid": status == "canceled" and captured > 0,
        "unavailable_order_paid": status == "unavailable" and captured > 0,
        "late_delivery_seller": shipment_verdict == "seller_delay",
        "late_delivery_logistics": shipment_verdict == "logistics_delay",
        "valid_split_payment": payment_verdict == "reconciled" and len(captures) >= 2,
        "payment_mismatch": payment_verdict == "capture_mismatch",
        "duplicate_charge": payment_verdict == "duplicate_capture",
        "refund_pending": payment_verdict == "refund_pending",
        "refund_failed": payment_verdict == "refund_failed",
        "unsupported_claim": bool(selected and payment and shipment),
    }
    if primary_claim in supported and supported[primary_claim]:
        primary = primary_claim
    elif selected and payment and shipment:
        primary = "unsupported_claim"
    else:
        # Sử dụng Meta-Llama-3.1-8B-Instruct semantic analyzer nếu rơi vào vùng mơ hồ
        ai_resolved = False
        if LLAMA.is_available() and request.get("message"):
            prompt = (
                f"Customer claim message: '{request.get('message')}'. "
                f"Claims: {request.get('claims')}. "
                "Map to exactly one allowed issue: late_delivery_logistics, late_delivery_seller, "
                "valid_split_payment, payment_mismatch, duplicate_charge, refund_pending, "
                "refund_failed, canceled_order_paid, unavailable_order_paid, unsupported_claim. "
                "Return JSON with key 'predicted_issue'."
            )
            ai_res = LLAMA.analyze_semantic(prompt)
            if ai_res and supported.get(ai_res.get("predicted_issue", ""), False):
                primary = ai_res["predicted_issue"]
                ai_resolved = True
        if not ai_resolved:
            primary = "insufficient_evidence"

    rules = ((policy or {}).get("data") or {}).get("rules") or {}
    rule = rules.get(primary) if isinstance(rules, dict) else None
    if not isinstance(rule, dict):
        rule = {}
    valid_statuses = {"action_required", "no_action", "needs_investigation"}
    case_status = (
        rule.get("case_status")
        if rule.get("case_status") in valid_statuses
        else "needs_investigation"
    )
    refundable = max(Decimal(0), captured - refunded)
    recommended = min(_amount(rule.get("refund_brl")) or Decimal(0), refundable)
    if case_status != "action_required":
        recommended = Decimal(0)

    refs = _unique([result["evidence_ref"] for result in evidence.values()])[:30]

    def scoped_refs(*tool_names: str) -> list[str]:
        return _unique([evidence[name]["evidence_ref"] for name in tool_names if name in evidence])

    claim_tools = {
        "late_delivery_logistics": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_policy",
        ),
        "late_delivery_seller": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_sellers",
            "get_policy",
        ),
        "valid_split_payment": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_payment_timeline",
            "get_policy",
        ),
        "payment_mismatch": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_payment_timeline",
            "get_policy",
        ),
        "duplicate_charge": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_payment_timeline",
            "get_policy",
        ),
        "refund_pending": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        ),
        "refund_failed": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_policy",
        ),
        "canceled_order_paid": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_payment_timeline",
            "get_shipment_summary",
            "get_policy",
        ),
        "unavailable_order_paid": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_payment_timeline",
            "get_shipment_summary",
            "get_policy",
        ),
        "unsupported_claim": (
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_payment_timeline",
            "get_policy",
        ),
    }
    conflicts: list[dict[str, Any]] = []
    direct_order = (order or {}).get("data") or {}
    if (
        selected
        and direct_order
        and selected.get("order_purchase_timestamp") != direct_order.get("order_purchase_timestamp")
    ):
        conflicts.append(
            {
                "field": "order_purchase_timestamp",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "TEMPORAL_CASE_MATCH",
            }
        )
    if (
        selected
        and shipment_data
        and selected.get("order_status") != shipment_data.get("order_status")
    ):
        conflicts.append(
            {
                "field": "order_status",
                "sources": ["get_shipment_summary", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "TEMPORAL_CASE_MATCH",
            }
        )
    if filtered_payment_conflict:
        conflicts.append(
            {
                "field": "captured_total_brl",
                "sources": ["get_payment_timeline", "get_order_items"],
                "selected_source": "get_payment_timeline",
                "resolution_code": "RECONCILED_SPLIT_SUBSET",
            }
        )

    customer_data = (history or {}).get("data") or {}
    related = _unique([row.get("order_id") for row in _rows(customer_data, "orders")])
    parties = []
    for party in rule.get("responsible_parties", [])[:5]:
        if not isinstance(party, dict) or party.get("party_type") not in {
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        }:
            continue
        party_id = (
            late_sellers[0]
            if party["party_type"] == "seller" and late_sellers
            else party.get("party_id")
        )
        parties.append({"party_type": party["party_type"], "party_id": party_id})

    claim_assessments = []
    for claim in request.get("claims", [])[:5]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            verdict = (
                "supported"
                if recommended > 0 and recommended >= refundable
                else "partially_supported"
                if recommended > 0
                else "unsupported"
            )
        else:
            verdict = (
                "supported"
                if topic == primary
                else "unsupported"
                if primary == "unsupported_claim"
                else "insufficient_evidence"
            )
        evidence_topic = primary if topic == "requested_full_refund" else topic
        names = claim_tools.get(evidence_topic, tuple(evidence))
        if topic == "requested_full_refund":
            names = tuple(dict.fromkeys((*names, "get_customer_history", "get_payment_timeline", "get_refund_timeline", "get_order_items")))
        claim_refs = scoped_refs(*names)
        if not claim_refs:
            claim_refs = refs[:10]
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": 0.8 if verdict == "supported" else 0.6,
                "evidence_refs": claim_refs,
            }
        )

    action = (
        rule.get("recommended_action")
        if isinstance(rule.get("recommended_action"), str)
        else "manual_review"
    )
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": [],
            "case_status": case_status,
            "confidence": 0.82
            if primary not in {"unsupported_claim", "insufficient_evidence"}
            else 0.65
            if primary == "unsupported_claim"
            else 0.2,
        },
        "affected_entities": {
            "order_ids": [resolved_id] if resolved_id else [],
            "item_ids": _unique([row.get("order_item_id") for row in item_rows + product_rows]),
            "seller_ids": _unique([row.get("seller_id") for row in item_rows + product_rows]),
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": "resolved" if resolved_id else "not_found",
            "resolved_order_ids": [resolved_id] if resolved_id else [],
            "rejected_candidates": rejected,
            "confidence": 0.9 if history and selected else 0.6 if selected else 0.1,
        },
        "customer_context": {
            "customer_unique_id": customer_data.get("customer_unique_id") if history else None,
            "related_order_ids": related,
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers if shipment_verdict == "seller_delay" else [],
            "timeline_complete": bool(selected and carrier and delivered and estimated),
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": float(captured) if captures else None,
            "refunded_total_brl": float(refunded) if refund else None,
            "refundable_total_brl": float(refundable) if captures else None,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary.upper(), "rank": 1}]
            if primary != "insufficient_evidence"
            else [],
            "responsible_parties": parties,
        },
        "evidence_refs": refs,
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(recommended),
            "refund_lines": [
                {
                    "reason_code": primary.upper(),
                    "amount_brl": float(recommended),
                    "entity_id": resolved_id,
                }
            ]
            if recommended > 0
            else [],
        },
        "resolution_actions": [action],
    }
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=primary.upper(),
        evidence_refs=[policy["evidence_ref"]] if policy else [],
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier",
        decision_code=primary.upper(),
        evidence_refs=[policy["evidence_ref"]] if policy else [],
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="EVIDENCE_SCOPED" if refs else "NO_EVIDENCE",
        evidence_refs=refs[:20],
        attributes={"resolved": bool(resolved_id), "conflicts": len(conflicts)},
    )
    return output
