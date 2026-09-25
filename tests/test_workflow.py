from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.context import InvestigationContext
from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, tuple(sorted(arguments.items()))))
        order_id = "a" * 32
        data: Any = {
            "get_customer_history": {
                "customer_unique_id": "customer-1",
                "orders": [{"order_id": order_id}],
            },
            "get_shipment_summary": {
                "order_id": order_id,
                "order_status": "delivered",
                "delivered_carrier_at": "2026-01-02T00:00:00Z",
                "delivered_customer_at": "2026-01-05T00:00:00Z",
                "estimated_delivery_at": "2026-01-04T00:00:00Z",
                "shipping_limits": [
                    {
                        "order_item_id": "item-1",
                        "seller_id": "seller-1",
                        "shipping_limit_at": "2026-01-03T00:00:00Z",
                    }
                ],
                "events": [
                    {
                        "event_type": "delivered_late",
                        "actor": "logistics_provider",
                        "status": "confirmed",
                    }
                ],
            },
            "get_sellers": [{"seller_id": "seller-1"}],
            "get_order_payments": [
                {
                    "payment_sequential": "1",
                    "payment_type": "credit_card",
                    "payment_value": "100.00",
                }
            ],
            "get_refund_timeline": {
                "events": [
                    {
                        "event_type": "refund_requested",
                        "amount_brl": "100.00",
                        "status": "pending",
                    }
                ]
            },
        }[tool_name]
        suffix = tool_name.replace("get_", "") + "_" + "x" * 30
        return {"evidence_ref": f"ev_{suffix}", "data": data}


def _trace(tmp_path: Path) -> tuple[TraceWriter, Contracts]:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    return TraceWriter(tmp_path / "trace.jsonl", contracts), contracts


def test_context_caches_identical_tool_calls(tmp_path: Path) -> None:
    trace, _ = _trace(tmp_path)
    gateway = FakeGateway()
    context = InvestigationContext("CASE_001", {}, gateway, trace)  # type: ignore[arg-type]

    async def exercise() -> None:
        await context.call("get_customer_history", actor="entity-agent", customer_unique_id="x")
        await context.call("get_customer_history", actor="entity-agent", customer_unique_id="x")

    asyncio.run(exercise())
    assert len(gateway.calls) == 1
    assert len(context.evidence_refs) == 1


def test_solve_case_is_schema_valid_and_observable(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    trace, contracts = _trace(tmp_path)
    gateway = FakeGateway()
    order_id = "a" * 32
    case = {
        "case_id": "CASE_001",
        "customer_unique_id_hint": "customer-1",
        "candidate_order_ids": [order_id, "candidate-1"],
        "customer_request": {
            "message": "Investigate a late delivery",
            "claims": [
                {"claim_id": "claim-1", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
    }
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    output = asyncio.run(solve_case(case, gateway, trace))  # type: ignore[arg-type]
    contracts.validate_output(output, "test output")
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")

    assert len(gateway.calls) == 5
    assert output["entity_resolution"]["rejected_candidates"] == ["candidate-1"]
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert any(
        party["party_type"] == "logistics_provider"
        for party in output["root_cause_analysis"]["responsible_parties"]
    )
    assert output["financial_resolution"]["recommended_refund_brl"] <= 100
    claim_refs = {
        ref for assessment in output["claim_assessments"] for ref in assessment["evidence_refs"]
    }
    assert claim_refs <= set(output["evidence_refs"])

    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert {
        "case_received",
        "task_assigned",
        "handoff",
        "tool_result_consumed",
        "verification_completed",
        "case_finalized",
    } <= {event["event_type"] for event in events}
