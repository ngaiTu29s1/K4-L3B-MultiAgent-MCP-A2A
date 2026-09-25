from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .context import InvestigationContext

ORDER_ID = re.compile(r"^[a-f0-9]{32}$")


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _amount(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


class EntityResolver:
    def __init__(self, context: InvestigationContext) -> None:
        self.context = context

    async def run(self) -> dict[str, Any]:
        case = self.context.case_data
        hint = str(case.get("customer_unique_id_hint") or "")
        self.context.trace.emit(
            case_id=self.context.case_id,
            event_type="handoff",
            actor="coordinator",
            target="entity-agent",
            decision_code="RESOLVE_CUSTOMER_ORDERS",
        )

        orders: list[dict[str, Any]] = []
        evidence_refs: list[str] = []
        try:
            evidence = await self.context.call(
                "get_customer_history", actor="entity-agent", customer_unique_id=hint
            )
            evidence_refs.append(evidence["evidence_ref"])
            data = evidence.get("data")
            if isinstance(data, dict):
                orders = _items(data.get("orders"))
                hint = str(data.get("customer_unique_id") or hint)
        except (KeyError, RuntimeError, TypeError, ValueError):
            pass

        valid_order_ids = {
            str(order["order_id"])
            for order in orders
            if isinstance(order.get("order_id"), str)
        }
        candidates = [
            candidate
            for candidate in case.get("candidate_order_ids", [])
            if isinstance(candidate, str)
        ]
        resolved = sorted(
            {
                candidate
                for candidate in candidates
                if candidate in valid_order_ids
                or (not candidate.startswith("candidate-") and ORDER_ID.fullmatch(candidate))
            }
        )
        rejected = sorted(set(candidates) - set(resolved))
        status = "resolved" if resolved else ("ambiguous" if candidates else "not_found")
        confidence = (
            1.0 if resolved and set(resolved) <= valid_order_ids else (0.6 if resolved else 0.0)
        )
        return {
            "output": {
                "status": status,
                "resolved_order_ids": resolved,
                "rejected_candidates": rejected,
                "confidence": confidence,
            },
            "customer_unique_id": hint or None,
            "related_order_ids": sorted(valid_order_ids),
            "evidence_refs": evidence_refs,
        }


class ShipmentSpecialist:
    def __init__(self, context: InvestigationContext) -> None:
        self.context = context

    async def run(self, order_ids: list[str]) -> dict[str, Any]:
        verdicts: list[str] = []
        late_seller_ids: set[str] = set()
        seller_ids: set[str] = set()
        item_ids: set[str] = set()
        shipment_ids: set[str] = set()
        evidence_refs: list[str] = []
        order_statuses: set[str] = set()
        conflicts: list[dict[str, Any]] = []
        complete: list[bool] = []

        for order_id in order_ids:
            summary: dict[str, Any] | None = None
            try:
                evidence = await self.context.call(
                    "get_shipment_summary", actor="shipment-agent", order_id=order_id
                )
                evidence_refs.append(evidence["evidence_ref"])
                if isinstance(evidence.get("data"), dict):
                    summary = evidence["data"]
            except (KeyError, RuntimeError, TypeError, ValueError):
                pass

            try:
                evidence = await self.context.call(
                    "get_sellers", actor="shipment-agent", order_id=order_id
                )
                evidence_refs.append(evidence["evidence_ref"])
                seller_ids.update(
                    str(item["seller_id"])
                    for item in _items(evidence.get("data"))
                    if isinstance(item.get("seller_id"), str)
                )
            except (KeyError, RuntimeError, TypeError, ValueError):
                pass

            if summary is None:
                verdicts.append("insufficient_evidence")
                complete.append(False)
                continue

            status = str(summary.get("order_status") or "").lower()
            if status:
                order_statuses.add(status)
            events = _items(summary.get("events"))
            limits = _items(summary.get("shipping_limits"))
            carrier_at = _time(summary.get("delivered_carrier_at"))
            customer_at = _time(summary.get("delivered_customer_at"))
            estimated_at = _time(summary.get("estimated_delivery_at"))
            complete.append(bool(customer_at and estimated_at and limits))

            latest_limit_by_seller: dict[str, datetime] = {}
            for limit in limits:
                seller_id = limit.get("seller_id")
                item_id = limit.get("order_item_id")
                if isinstance(seller_id, str):
                    seller_ids.add(seller_id)
                    limit_at = _time(limit.get("shipping_limit_at"))
                    if limit_at and (
                        seller_id not in latest_limit_by_seller
                        or limit_at > latest_limit_by_seller[seller_id]
                    ):
                        latest_limit_by_seller[seller_id] = limit_at
                if isinstance(item_id, str):
                    item_ids.add(item_id)
            for event in events:
                shipment_id = event.get("shipment_id")
                if isinstance(shipment_id, str):
                    shipment_ids.add(shipment_id)

            dated_verdict = "insufficient_evidence"
            delayed_sellers = {
                seller_id
                for seller_id, limit_at in latest_limit_by_seller.items()
                if carrier_at and carrier_at > limit_at
            }
            if delayed_sellers:
                dated_verdict = "seller_delay"
            elif customer_at and estimated_at:
                dated_verdict = (
                    "logistics_delay" if customer_at > estimated_at else "on_time"
                )

            event_verdict: str | None = None
            for event in events:
                event_type = str(event.get("event_type") or "").lower()
                actor = str(event.get("actor") or "").lower()
                event_status = str(event.get("status") or "").lower()
                if event_status and event_status not in {"confirmed", "completed", "success"}:
                    continue
                if "return" in event_type:
                    event_verdict = "returned"
                elif "lost" in event_type:
                    event_verdict = "lost"
                elif "late" in event_type and "seller" in actor:
                    event_verdict = "seller_delay"
                elif "late" in event_type and "logistics" in actor:
                    event_verdict = "logistics_delay"

            if "return" in status:
                verdict = "returned"
            elif status in {"lost", "unavailable"}:
                verdict = "lost"
            else:
                verdict = event_verdict or dated_verdict
            if verdict == "seller_delay":
                late_seller_ids.update(delayed_sellers or latest_limit_by_seller)
            if event_verdict and dated_verdict not in {event_verdict, "insufficient_evidence"}:
                conflicts.append(
                    {
                        "field": f"shipment_analysis.verdict:{order_id}",
                        "sources": ["shipment.events", "shipment.timeline"],
                        "selected_source": "shipment.events",
                        "resolution_code": "AUTHORITATIVE_EVENT",
                    }
                )
            verdicts.append(verdict)

        unique_verdicts = set(verdicts) - {"insufficient_evidence"}
        verdict = (
            "insufficient_evidence"
            if not unique_verdicts
            else next(iter(unique_verdicts))
            if len(unique_verdicts) == 1
            else "conflicting"
        )
        return {
            "output": {
                "verdict": verdict,
                "late_seller_ids": sorted(late_seller_ids),
                "timeline_complete": bool(complete) and all(complete),
            },
            "seller_ids": sorted(seller_ids),
            "item_ids": sorted(item_ids),
            "shipment_ids": sorted(shipment_ids),
            "order_statuses": sorted(order_statuses),
            "evidence_refs": evidence_refs,
            "conflicts": conflicts[:5],
        }


class PaymentSpecialist:
    def __init__(self, context: InvestigationContext) -> None:
        self.context = context

    async def run(self, order_ids: list[str]) -> dict[str, Any]:
        captured = Decimal("0")
        refunded = Decimal("0")
        payment_seen = False
        refund_seen = False
        pending = failed = completed = False
        payment_keys: list[tuple[str, str, Decimal]] = []
        amounts_by_sequence: dict[tuple[str, str], set[Decimal]] = defaultdict(set)
        payment_types: set[str] = set()
        payment_references: set[str] = set()
        evidence_refs: list[str] = []

        for order_id in order_ids:
            try:
                evidence = await self.context.call(
                    "get_order_payments", actor="payment-agent", order_id=order_id
                )
                evidence_refs.append(evidence["evidence_ref"])
                payment_seen = True
                for payment in _items(evidence.get("data")):
                    status = str(payment.get("status") or "").lower()
                    if status and status not in {
                        "approved",
                        "captured",
                        "completed",
                        "confirmed",
                        "success",
                        "succeeded",
                    }:
                        continue
                    value = _amount(payment.get("payment_value", payment.get("amount_brl")))
                    captured += value
                    sequence = str(payment.get("payment_sequential") or "")
                    payment_type = str(payment.get("payment_type") or "")
                    payment_keys.append((sequence, payment_type, value))
                    amounts_by_sequence[(sequence, payment_type)].add(value)
                    if payment_type:
                        payment_types.add(payment_type)
                    reference = payment.get("payment_reference")
                    if isinstance(reference, str):
                        payment_references.add(reference)
            except (KeyError, RuntimeError, TypeError, ValueError):
                pass

            try:
                evidence = await self.context.call(
                    "get_refund_timeline", actor="payment-agent", order_id=order_id
                )
                evidence_refs.append(evidence["evidence_ref"])
                refund_seen = True
                data = evidence.get("data")
                events = _items(data.get("events")) if isinstance(data, dict) else _items(data)
                for event in events:
                    event_type = str(event.get("event_type") or "").lower()
                    status = str(event.get("status") or "").lower()
                    value = _amount(event.get("amount_brl", event.get("refund_amount")))
                    if status in {
                        "confirmed",
                        "completed",
                        "success",
                        "succeeded",
                        "refunded",
                    } or any(word in event_type for word in ("completed", "refunded", "succeeded")):
                        refunded += value
                        completed = True
                    elif status in {
                        "failed",
                        "rejected",
                        "cancelled",
                        "canceled",
                    } or any(word in event_type for word in ("failed", "rejected", "cancelled")):
                        failed = True
                    elif "request" in event_type or status in {"pending", "processing"}:
                        pending = True
                    reference = event.get("payment_reference") or event.get("refund_id")
                    if isinstance(reference, str):
                        payment_references.add(reference)
            except (KeyError, RuntimeError, TypeError, ValueError):
                pass

        refundable = max(captured - refunded, Decimal("0"))
        duplicate = any(count > 1 for count in Counter(payment_keys).values())
        mismatch = any(len(values) > 1 for values in amounts_by_sequence.values())
        if not payment_seen:
            verdict = "insufficient_evidence"
        elif pending:
            verdict = "refund_pending"
        elif failed:
            verdict = "refund_failed"
        elif completed:
            verdict = "refunded"
        elif duplicate:
            verdict = "duplicate_capture"
        elif mismatch:
            verdict = "capture_mismatch"
        else:
            verdict = "reconciled"
        return {
            "output": {
                "verdict": verdict,
                "captured_total_brl": _money(captured) if payment_seen else None,
                "refunded_total_brl": _money(refunded) if refund_seen else None,
                "refundable_total_brl": _money(refundable) if payment_seen else None,
            },
            "payment_references": sorted(payment_references),
            "payment_types": sorted(payment_types),
            "payment_count": len(payment_keys),
            "evidence_refs": evidence_refs,
        }
