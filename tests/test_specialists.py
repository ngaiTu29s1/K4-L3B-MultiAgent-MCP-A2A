from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.entity_customer import CaseEvidenceCache
from student_agent.specialists import (
    investigate_order_product,
    investigate_payment_refund,
    investigate_shipment,
)


class FakeGateway:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        response = self.responses.get(tool_name)
        if response is None:
            raise RuntimeError(f"unavailable: {tool_name}")
        return response


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def evidence(number: int, domain: str, data: Any) -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{number:024d}",
        "result_hash": f"sha256:{number:064x}",
        "domain": domain,
        "data": data,
    }


def test_order_product_normalizes_entities_and_reuses_case_cache() -> None:
    gateway = FakeGateway(
        {
            "get_order": evidence(1, "order", {"order_id": "ORDER_1", "status": "canceled"}),
            "get_order_items": evidence(
                2,
                "item",
                {
                    "items": [
                        {
                            "item_id": "ITEM_1",
                            "seller_id": "SELLER_1",
                            "product_id": "PRODUCT_1",
                        }
                    ]
                },
            ),
            "get_product_context": evidence(
                3,
                "product",
                {
                    "products": [
                        {"product_id": "PRODUCT_1", "product_category_name": "books"}
                    ]
                },
            ),
        }
    )
    trace = FakeTrace()
    cache = CaseEvidenceCache("CASE_1")

    async def exercise() -> tuple[Any, Any]:
        first = await investigate_order_product(
            case_id="CASE_1",
            order_id="ORDER_1",
            gateway=gateway,  # type: ignore[arg-type]
            trace=trace,  # type: ignore[arg-type]
            evidence_cache=cache,
        )
        second = await investigate_order_product(
            case_id="CASE_1",
            order_id="ORDER_1",
            gateway=gateway,  # type: ignore[arg-type]
            trace=trace,  # type: ignore[arg-type]
            evidence_cache=cache,
        )
        return first, second

    first, second = asyncio.run(exercise())

    assert first == second
    assert first.canceled is True
    assert first.item_ids == ("ITEM_1",)
    assert first.seller_ids == ("SELLER_1",)
    assert first.product_ids == ("PRODUCT_1",)
    assert first.product_categories == ("books",)
    assert len(gateway.calls) == 3
    consumed = [event for event in trace.events if event["event_type"] == "tool_result_consumed"]
    assert len(consumed) == 6


def test_shipment_attributes_seller_delay_from_timeline() -> None:
    gateway = FakeGateway(
        {
            "get_shipment_summary": evidence(
                4,
                "shipment",
                {
                    "shipment": {
                        "shipment_id": "SHIP_1",
                        "order_delivered_carrier_date": "2018-02-05T12:00:00Z",
                        "shipping_limit_date": "2018-02-04T12:00:00Z",
                        "order_delivered_customer_date": "2018-02-10T12:00:00Z",
                        "order_estimated_delivery_date": "2018-02-12T12:00:00Z",
                    }
                },
            ),
            "get_sellers": evidence(5, "seller", {"sellers": [{"seller_id": "SELLER_1"}]}),
        }
    )
    trace = FakeTrace()

    result = asyncio.run(
        investigate_shipment(
            case_id="CASE_1",
            order_id="ORDER_1",
            gateway=gateway,  # type: ignore[arg-type]
            trace=trace,  # type: ignore[arg-type]
        )
    )

    assert result.verdict == "seller_delay"
    assert result.late_seller_ids == ("SELLER_1",)
    assert result.timeline_complete is True
    assert result.data_conflicts == ()


def test_shipment_preserves_conflicting_authoritative_sources() -> None:
    gateway = FakeGateway(
        {
            "get_shipment_summary": evidence(
                12,
                "shipment",
                {
                    "order_status": "delivered",
                    "delivered_carrier_at": "2018-05-13T09:00:00-03:00",
                    "delivered_customer_at": "2018-05-20T09:00:00-03:00",
                    "estimated_delivery_at": "2018-05-21T09:00:00-03:00",
                    "shipping_limits": [
                        {"seller_id": "SELLER_1", "shipping_limit_at": "2018-05-14T09:00:00-03:00"},
                        {"seller_id": "SELLER_1", "shipping_limit_at": "2017-12-23T09:00:00-03:00"},
                    ],
                    "events": [
                        {
                            "event_type": "delivered_late",
                            "actor": "logistics_provider",
                            "status": "confirmed",
                        }
                    ],
                },
            ),
            "get_sellers": evidence(13, "seller", [{"seller_id": "SELLER_1"}]),
        }
    )

    result = asyncio.run(
        investigate_shipment(
            case_id="CASE_1",
            order_id="ORDER_1",
            gateway=gateway,  # type: ignore[arg-type]
            trace=FakeTrace(),  # type: ignore[arg-type]
        )
    )

    assert result.verdict == "conflicting"
    assert result.timeline_complete is True
    assert len(result.data_conflicts) == 2
    assert result.late_seller_ids == ()


def test_payment_refund_calculates_pending_refund_totals() -> None:
    gateway = FakeGateway(
        {
            "get_order_payments": evidence(
                6,
                "payment",
                {
                    "payments": [
                        {
                            "payment_reference": "PAY_1",
                            "status": "captured",
                            "payment_value": 125.50,
                            "order_total_brl": 125.50,
                        }
                    ]
                },
            ),
            "get_payment_timeline": evidence(
                7,
                "payment",
                {"events": [{"payment_reference": "PAY_1", "status": "captured"}]},
            ),
            "get_refund_timeline": evidence(
                8,
                "refund",
                {
                    "refunds": [
                        {"status": "completed", "refund_amount_brl": 25.50},
                        {"status": "pending", "refund_amount_brl": 100.00},
                    ]
                },
            ),
        }
    )
    trace = FakeTrace()

    result = asyncio.run(
        investigate_payment_refund(
            case_id="CASE_1",
            order_id="ORDER_1",
            gateway=gateway,  # type: ignore[arg-type]
            trace=trace,  # type: ignore[arg-type]
        )
    )

    assert result.verdict == "refund_pending"
    assert result.captured_total_brl == 125.50
    assert result.refunded_total_brl == 25.50
    assert result.refundable_total_brl == 100.00
    assert result.payment_references == ("PAY_1",)


def test_payment_detects_duplicate_capture_from_authoritative_timeline() -> None:
    gateway = FakeGateway(
        {
            "get_order_payments": evidence(
                9,
                "payment",
                {"payments": [{"payment_reference": "PAY_1", "payment_value": 80}]},
            ),
            "get_payment_timeline": evidence(
                10,
                "payment",
                {
                    "events": [
                        {"payment_reference": "PAY_1", "status": "captured"},
                        {"payment_reference": "PAY_1", "status": "captured"},
                    ]
                },
            ),
            "get_refund_timeline": evidence(11, "refund", {"refunds": []}),
        }
    )

    result = asyncio.run(
        investigate_payment_refund(
            case_id="CASE_1",
            order_id="ORDER_1",
            gateway=gateway,  # type: ignore[arg-type]
            trace=FakeTrace(),  # type: ignore[arg-type]
        )
    )

    assert result.verdict == "duplicate_capture"


def test_specialist_rejects_cross_case_cache() -> None:
    with pytest.raises(ValueError, match="another case"):
        asyncio.run(
            investigate_shipment(
                case_id="CASE_2",
                order_id="ORDER_1",
                gateway=FakeGateway({}),  # type: ignore[arg-type]
                trace=FakeTrace(),  # type: ignore[arg-type]
                evidence_cache=CaseEvidenceCache("CASE_1"),
            )
        )
