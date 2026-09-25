"""Deterministic commerce specialists for the L3B workflow.

The specialists in this module only normalize authoritative MCP evidence.  They
do not choose the final primary issue, responsible party, or refund action;
those decisions belong to the policy/coordinator and verifier stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from .entity_customer import COORDINATOR_ACTOR, CaseEvidenceCache
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ORDER_PRODUCT_ACTOR = "order-product-agent"
SHIPMENT_ACTOR = "shipment-agent"
PAYMENT_REFUND_ACTOR = "payment-refund-agent"

SpecialistStatus = Literal["completed", "partial", "failed"]
ShipmentVerdict = Literal[
    "on_time",
    "seller_delay",
    "logistics_delay",
    "lost",
    "returned",
    "conflicting",
    "insufficient_evidence",
]
PaymentVerdict = Literal[
    "reconciled",
    "capture_mismatch",
    "duplicate_capture",
    "refund_pending",
    "refund_failed",
    "refunded",
    "insufficient_evidence",
]


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def _records(data: Any, collection_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return record-like objects from common MCP response shapes."""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in collection_keys:
        nested = data.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            return [nested]
    return [data]


def _strings(records: list[dict[str, Any]], keys: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for record in records:
        for key in keys:
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value)
            elif isinstance(value, list):
                values.extend(item for item in value if isinstance(item, str) and item.strip())
    return _unique(values)


def _first(records: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for record in records:
        for key in keys:
            value = record.get(key)
            if value is not None and value != "":
                return value
    return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _money(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.01")))


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


async def _consume_order_tool(
    *,
    cache: CaseEvidenceCache,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    actor: str,
    tool_name: str,
    case_id: str,
    order_id: str,
) -> dict[str, Any] | None:
    """Call one order-scoped tool and trace a successful evidence consumption."""
    try:
        evidence = await cache.call(
            gateway,
            tool_name,
            case_id=case_id,
            order_id=order_id,
        )
    except RuntimeError:
        return None
    evidence_ref = evidence["evidence_ref"]
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence_ref],
    )
    return evidence


def _emit_assignment(trace: TraceWriter, case_id: str, actor: str, order_id: str) -> None:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor=COORDINATOR_ACTOR,
        target=actor,
        attributes={"order_id": order_id},
    )


def _emit_handoff(
    trace: TraceWriter,
    *,
    case_id: str,
    actor: str,
    status: SpecialistStatus,
    evidence_refs: tuple[str, ...],
) -> None:
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target=COORDINATOR_ACTOR,
        decision_code=f"SPECIALIST_{status.upper()}",
        evidence_refs=list(evidence_refs),
    )


@dataclass(frozen=True)
class OrderProductHandoff:
    case_id: str
    order_id: str
    status: SpecialistStatus
    order_status: str | None
    item_ids: tuple[str, ...]
    seller_ids: tuple[str, ...]
    product_ids: tuple[str, ...]
    product_categories: tuple[str, ...]
    canceled: bool
    unavailable: bool
    confidence: float
    evidence_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source_agent": ORDER_PRODUCT_ACTOR,
            "target_agent": COORDINATOR_ACTOR,
            "task_type": "analyze_order_product",
            "status": self.status,
            "decision_code": f"ORDER_PRODUCT_{self.status.upper()}",
            "entity_ids": {
                "order_ids": [self.order_id],
                "item_ids": list(self.item_ids),
                "seller_ids": list(self.seller_ids),
                "product_ids": list(self.product_ids),
            },
            "facts": {
                "order_status": self.order_status,
                "canceled": self.canceled,
                "unavailable": self.unavailable,
                "product_categories": list(self.product_categories),
            },
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class ShipmentHandoff:
    case_id: str
    order_id: str
    status: SpecialistStatus
    verdict: ShipmentVerdict
    shipment_ids: tuple[str, ...]
    late_seller_ids: tuple[str, ...]
    timeline_complete: bool
    data_conflicts: tuple[dict[str, Any], ...]
    confidence: float
    evidence_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source_agent": SHIPMENT_ACTOR,
            "target_agent": COORDINATOR_ACTOR,
            "task_type": "analyze_shipment",
            "status": self.status,
            "decision_code": self.verdict.upper(),
            "entity_ids": {
                "order_ids": [self.order_id],
                "shipment_ids": list(self.shipment_ids),
                "seller_ids": list(self.late_seller_ids),
            },
            "facts": {
                "verdict": self.verdict,
                "timeline_complete": self.timeline_complete,
            },
            "data_conflicts": list(self.data_conflicts),
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class PaymentRefundHandoff:
    case_id: str
    order_id: str
    status: SpecialistStatus
    verdict: PaymentVerdict
    captured_total_brl: float | None
    refunded_total_brl: float | None
    refundable_total_brl: float | None
    payment_references: tuple[str, ...]
    confidence: float
    evidence_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source_agent": PAYMENT_REFUND_ACTOR,
            "target_agent": COORDINATOR_ACTOR,
            "task_type": "analyze_payment_refund",
            "status": self.status,
            "decision_code": self.verdict.upper(),
            "entity_ids": {
                "order_ids": [self.order_id],
                "payment_references": list(self.payment_references),
            },
            "facts": {
                "verdict": self.verdict,
                "captured_total_brl": self.captured_total_brl,
                "refunded_total_brl": self.refunded_total_brl,
                "refundable_total_brl": self.refundable_total_brl,
            },
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }


async def investigate_order_product(
    *,
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    evidence_cache: CaseEvidenceCache | None = None,
) -> OrderProductHandoff:
    """Load and normalize order, item, and product evidence for one order."""
    cache = evidence_cache or CaseEvidenceCache(case_id)
    if cache.case_id != case_id:
        raise ValueError("order agent received an evidence cache for another case")
    _emit_assignment(trace, case_id, ORDER_PRODUCT_ACTOR, order_id)

    evidence_by_tool: dict[str, dict[str, Any]] = {}
    for tool_name in ("get_order", "get_order_items", "get_product_context"):
        evidence = await _consume_order_tool(
            cache=cache,
            gateway=gateway,
            trace=trace,
            actor=ORDER_PRODUCT_ACTOR,
            tool_name=tool_name,
            case_id=case_id,
            order_id=order_id,
        )
        if evidence is not None:
            evidence_by_tool[tool_name] = evidence

    order_records = _records(
        evidence_by_tool.get("get_order", {}).get("data"), ("order", "orders")
    )
    item_records = _records(
        evidence_by_tool.get("get_order_items", {}).get("data"), ("items", "order_items")
    )
    product_records = _records(
        evidence_by_tool.get("get_product_context", {}).get("data"),
        ("products", "product_context", "items"),
    )
    order_status_value = _first(order_records, ("order_status", "status"))
    order_status = str(order_status_value).strip().lower() if order_status_value else None
    item_ids = _strings(item_records, ("order_item_id", "item_id"))
    seller_ids = _strings([*item_records, *product_records], ("seller_id", "seller_ids"))
    product_ids = _strings([*item_records, *product_records], ("product_id", "product_ids"))
    product_categories = _strings(
        product_records,
        ("product_category_name", "product_category", "category"),
    )
    normalized_status = order_status or ""
    references = _unique(
        [evidence["evidence_ref"] for evidence in evidence_by_tool.values()]
    )
    if len(evidence_by_tool) == 3:
        status: SpecialistStatus = "completed"
        confidence = 0.98 if order_status else 0.85
    elif evidence_by_tool:
        status = "partial"
        confidence = 0.55
    else:
        status = "failed"
        confidence = 0.0
    handoff = OrderProductHandoff(
        case_id=case_id,
        order_id=order_id,
        status=status,
        order_status=order_status,
        item_ids=item_ids,
        seller_ids=seller_ids,
        product_ids=product_ids,
        product_categories=product_categories,
        canceled=normalized_status in {"canceled", "cancelled"},
        unavailable=normalized_status == "unavailable",
        confidence=confidence,
        evidence_refs=references,
    )
    _emit_handoff(
        trace,
        case_id=case_id,
        actor=ORDER_PRODUCT_ACTOR,
        status=status,
        evidence_refs=references,
    )
    return handoff


def _explicit_shipment_verdict(records: list[dict[str, Any]]) -> ShipmentVerdict | None:
    raw = _first(records, ("verdict", "shipment_verdict", "delay_responsibility"))
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower().replace("-", "_").replace(" ", "_")
    aliases: dict[str, ShipmentVerdict] = {
        "on_time": "on_time",
        "seller": "seller_delay",
        "seller_delay": "seller_delay",
        "logistics": "logistics_delay",
        "carrier_delay": "logistics_delay",
        "logistics_delay": "logistics_delay",
        "lost": "lost",
        "returned": "returned",
        "conflicting": "conflicting",
    }
    return aliases.get(normalized)


def _shipment_verdict(
    records: list[dict[str, Any]],
    shipping_limits: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> tuple[ShipmentVerdict, bool, tuple[dict[str, Any], ...]]:
    explicit = _explicit_shipment_verdict(records)
    order_status_value = _first(records, ("shipment_status", "order_status", "status"))
    order_status = str(order_status_value).strip().lower() if order_status_value else ""
    if "lost" in order_status:
        return "lost", False, ()
    if "return" in order_status:
        return "returned", False, ()
    if explicit is not None:
        complete = explicit in {"on_time", "seller_delay", "logistics_delay"}
        return explicit, complete, ()

    carrier_date = _timestamp(
        _first(
            records,
            ("order_delivered_carrier_date", "delivered_carrier_at", "carrier_received_at"),
        )
    )
    customer_date = _timestamp(
        _first(
            records,
            ("order_delivered_customer_date", "delivered_customer_at", "delivered_at"),
        )
    )
    limit_values = [
        value
        for value in (
            _timestamp(
                _first(
                    [record],
                    ("shipping_limit_at", "shipping_limit_date", "seller_shipping_deadline"),
                )
            )
            for record in shipping_limits
        )
        if value is not None
    ]
    direct_limit = _timestamp(
        _first(records, ("shipping_limit_at", "shipping_limit_date", "seller_shipping_deadline"))
    )
    if direct_limit is not None:
        limit_values.append(direct_limit)
    estimated_delivery = _timestamp(
        _first(records, ("order_estimated_delivery_date", "estimated_delivery_at"))
    )
    timeline_complete = all(
        value is not None
        for value in (carrier_date, customer_date, estimated_delivery)
    ) and bool(limit_values)

    event_verdict: ShipmentVerdict | None = None
    for event in events:
        event_type = str(event.get("event_type", "")).strip().lower()
        actor = str(event.get("actor", "")).strip().lower()
        status = str(event.get("status", "")).strip().lower()
        if status in {"rejected", "invalid", "unconfirmed"}:
            continue
        if "late" in event_type:
            if actor == "seller":
                event_verdict = "seller_delay"
            elif actor in {"logistics", "logistics_provider", "carrier"}:
                event_verdict = "logistics_delay"

    date_verdict: ShipmentVerdict | None = None
    seller_relations = {
        carrier_date > limit for limit in limit_values if carrier_date is not None
    }
    if seller_relations == {True}:
        date_verdict = "seller_delay"
    elif customer_date and estimated_delivery:
        date_verdict = (
            "logistics_delay" if customer_date > estimated_delivery else "on_time"
        )

    conflicts: list[dict[str, Any]] = []
    if len(seller_relations) > 1:
        conflicts.append(
            {
                "field": "shipment.shipping_limit_at",
                "sources": [
                    "shipment_summary.shipping_limits[0]",
                    "shipment_summary.shipping_limits[1]",
                ],
                "selected_source": None,
                "resolution_code": "CONFLICTING_SHIPPING_LIMITS",
            }
        )
    if event_verdict and date_verdict and event_verdict != date_verdict:
        conflicts.append(
            {
                "field": "shipment.verdict",
                "sources": ["shipment_summary.timeline", "shipment_summary.events"],
                "selected_source": None,
                "resolution_code": "UNRESOLVED_SOURCE_CONFLICT",
            }
        )
    if conflicts:
        return "conflicting", timeline_complete, tuple(conflicts)
    if event_verdict is not None:
        return event_verdict, timeline_complete, ()
    if date_verdict is not None:
        return date_verdict, timeline_complete, ()
    return "insufficient_evidence", False, ()


async def investigate_shipment(
    *,
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    evidence_cache: CaseEvidenceCache | None = None,
) -> ShipmentHandoff:
    """Build the shipment timeline and attribute a delay only when evidence supports it."""
    cache = evidence_cache or CaseEvidenceCache(case_id)
    if cache.case_id != case_id:
        raise ValueError("shipment agent received an evidence cache for another case")
    _emit_assignment(trace, case_id, SHIPMENT_ACTOR, order_id)

    evidence_by_tool: dict[str, dict[str, Any]] = {}
    for tool_name in ("get_shipment_summary", "get_sellers"):
        evidence = await _consume_order_tool(
            cache=cache,
            gateway=gateway,
            trace=trace,
            actor=SHIPMENT_ACTOR,
            tool_name=tool_name,
            case_id=case_id,
            order_id=order_id,
        )
        if evidence is not None:
            evidence_by_tool[tool_name] = evidence

    shipment_data = evidence_by_tool.get("get_shipment_summary", {}).get("data")
    shipment_records = _records(
        shipment_data,
        ("shipments", "shipment", "timeline"),
    )
    shipping_limits = (
        _records(shipment_data.get("shipping_limits"), ())
        if isinstance(shipment_data, dict)
        else []
    )
    shipment_events = (
        _records(shipment_data.get("events"), ()) if isinstance(shipment_data, dict) else []
    )
    seller_records = _records(
        evidence_by_tool.get("get_sellers", {}).get("data"), ("sellers", "seller")
    )
    verdict, timeline_complete, data_conflicts = _shipment_verdict(
        shipment_records, shipping_limits, shipment_events
    )
    seller_ids = _strings(
        [*shipment_records, *shipping_limits, *seller_records],
        ("seller_id", "seller_ids"),
    )
    shipment_ids = _strings(
        shipment_records, ("shipment_id", "shipping_id", "tracking_code")
    )
    references = _unique(
        [evidence["evidence_ref"] for evidence in evidence_by_tool.values()]
    )
    if verdict == "insufficient_evidence":
        status: SpecialistStatus = "partial" if evidence_by_tool else "failed"
        confidence = 0.25 if evidence_by_tool else 0.0
    elif len(evidence_by_tool) == 2:
        status = "completed"
        confidence = 0.95
    else:
        status = "partial"
        confidence = 0.75
    handoff = ShipmentHandoff(
        case_id=case_id,
        order_id=order_id,
        status=status,
        verdict=verdict,
        shipment_ids=shipment_ids,
        late_seller_ids=seller_ids if verdict == "seller_delay" else (),
        timeline_complete=timeline_complete,
        data_conflicts=data_conflicts,
        confidence=confidence,
        evidence_refs=references,
    )
    _emit_handoff(
        trace,
        case_id=case_id,
        actor=SHIPMENT_ACTOR,
        status=status,
        evidence_refs=references,
    )
    return handoff


def _status_text(record: dict[str, Any]) -> str:
    raw = _first([record], ("status", "payment_status", "refund_status", "event_type"))
    return str(raw).strip().lower().replace("-", "_").replace(" ", "_") if raw else ""


def _amount(record: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    return _decimal(_first([record], keys))


def _captured_total(records: list[dict[str, Any]]) -> Decimal | None:
    amount_keys = (
        "captured_amount_brl",
        "captured_amount",
        "payment_value",
        "amount_brl",
        "amount",
        "value",
    )
    rejected = {"failed", "declined", "cancelled", "canceled", "voided", "refused"}
    values: list[Decimal] = []
    for record in records:
        status = _status_text(record)
        value = _amount(record, amount_keys)
        if value is not None and status not in rejected:
            values.append(value)
    return sum(values, Decimal()) if values else None


def _refund_state(records: list[dict[str, Any]]) -> tuple[Decimal | None, bool, bool]:
    amount_keys = ("refund_amount_brl", "refund_amount", "amount_brl", "amount", "value")
    completed_words = {"refunded", "completed", "succeeded", "success", "settled"}
    pending_words = {"pending", "requested", "processing", "initiated"}
    failed_words = {"failed", "rejected", "declined", "cancelled", "canceled"}
    completed_values: list[Decimal] = []
    pending = False
    failed = False
    for record in records:
        status = _status_text(record)
        tokens = set(status.split("_"))
        pending = pending or bool(tokens & pending_words)
        failed = failed or bool(tokens & failed_words)
        if tokens & completed_words:
            value = _amount(record, amount_keys)
            if value is not None:
                completed_values.append(value)
    total = sum(completed_values, Decimal()) if completed_values else None
    return total, pending, failed


def _has_duplicate_capture(records: list[dict[str, Any]]) -> bool:
    seen_references: set[str] = set()
    for record in records:
        if record.get("is_duplicate") is True or record.get("duplicate_of"):
            return True
        status = _status_text(record)
        if "duplicate" in status and ("capture" in status or "charge" in status):
            return True
        if "capture" not in status and status not in {"captured", "paid", "settled"}:
            continue
        reference = _first(
            [record], ("payment_reference", "transaction_id", "payment_id", "charge_id")
        )
        if isinstance(reference, str) and reference:
            if reference in seen_references:
                return True
            seen_references.add(reference)
    return False


def _expected_total(records: list[dict[str, Any]]) -> Decimal | None:
    value = _first(
        records,
        ("expected_total_brl", "order_total_brl", "expected_amount", "order_total"),
    )
    return _decimal(value)


async def investigate_payment_refund(
    *,
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    evidence_cache: CaseEvidenceCache | None = None,
) -> PaymentRefundHandoff:
    """Reconcile captured payments and refund lifecycle for one resolved order."""
    cache = evidence_cache or CaseEvidenceCache(case_id)
    if cache.case_id != case_id:
        raise ValueError("payment agent received an evidence cache for another case")
    _emit_assignment(trace, case_id, PAYMENT_REFUND_ACTOR, order_id)

    evidence_by_tool: dict[str, dict[str, Any]] = {}
    for tool_name in (
        "get_order_payments",
        "get_payment_timeline",
        "get_refund_timeline",
    ):
        evidence = await _consume_order_tool(
            cache=cache,
            gateway=gateway,
            trace=trace,
            actor=PAYMENT_REFUND_ACTOR,
            tool_name=tool_name,
            case_id=case_id,
            order_id=order_id,
        )
        if evidence is not None:
            evidence_by_tool[tool_name] = evidence

    payment_records = _records(
        evidence_by_tool.get("get_order_payments", {}).get("data"),
        ("payments", "order_payments", "transactions"),
    )
    payment_timeline = _records(
        evidence_by_tool.get("get_payment_timeline", {}).get("data"),
        ("events", "timeline", "payments", "transactions"),
    )
    refund_records = _records(
        evidence_by_tool.get("get_refund_timeline", {}).get("data"),
        ("refunds", "events", "timeline"),
    )
    captured = _captured_total(payment_records)
    refunded, refund_pending, refund_failed = _refund_state(refund_records)
    refunded_for_math = refunded or Decimal()
    refundable = max(captured - refunded_for_math, Decimal()) if captured is not None else None
    expected = _expected_total(payment_records)

    if not payment_records and not payment_timeline and not refund_records:
        verdict: PaymentVerdict = "insufficient_evidence"
    elif _has_duplicate_capture(payment_timeline):
        verdict = "duplicate_capture"
    elif refund_failed:
        verdict = "refund_failed"
    elif refund_pending:
        verdict = "refund_pending"
    elif refunded is not None and refunded > 0:
        verdict = "refunded"
    elif (
        captured is not None
        and expected is not None
        and abs(captured - expected) > Decimal("0.01")
    ):
        verdict = "capture_mismatch"
    elif captured is not None:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"

    payment_references = _strings(
        [*payment_records, *payment_timeline],
        ("payment_reference", "transaction_id", "payment_id", "charge_id"),
    )
    references = _unique(
        [evidence["evidence_ref"] for evidence in evidence_by_tool.values()]
    )
    if verdict == "insufficient_evidence":
        status: SpecialistStatus = "partial" if evidence_by_tool else "failed"
        confidence = 0.25 if evidence_by_tool else 0.0
    elif len(evidence_by_tool) == 3:
        status = "completed"
        confidence = 0.97
    else:
        status = "partial"
        confidence = 0.7
    handoff = PaymentRefundHandoff(
        case_id=case_id,
        order_id=order_id,
        status=status,
        verdict=verdict,
        captured_total_brl=_money(captured),
        refunded_total_brl=_money(refunded),
        refundable_total_brl=_money(refundable),
        payment_references=payment_references,
        confidence=confidence,
        evidence_refs=references,
    )
    _emit_handoff(
        trace,
        case_id=case_id,
        actor=PAYMENT_REFUND_ACTOR,
        status=status,
        evidence_refs=references,
    )
    return handoff
