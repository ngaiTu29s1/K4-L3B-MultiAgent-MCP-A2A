from __future__ import annotations

from typing import Any

from .context import InvestigationContext
from .llm_coordinator import LLMCoordinator
from .mcp_gateway import EvidenceGateway
from .specialists import EntityResolver, PaymentSpecialist, ShipmentSpecialist
from .trace import TraceWriter
from .verifier import Verifier


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    context = InvestigationContext(case["case_id"], case, gateway, trace)
    trace.emit(
        case_id=context.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="RESOLVE_ENTITIES",
    )
    entity = await EntityResolver(context).run()
    order_ids = entity["output"]["resolved_order_ids"]

    trace.emit(
        case_id=context.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment-agent",
        decision_code="ANALYZE_SHIPMENT",
    )
    trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="entity-agent",
        target="shipment-agent",
        decision_code="ENTITIES_RESOLVED",
    )
    shipment = await ShipmentSpecialist(context).run(order_ids)

    trace.emit(
        case_id=context.case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment-agent",
        decision_code="RECONCILE_PAYMENT",
    )
    trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="entity-agent",
        target="payment-agent",
        decision_code="ENTITIES_RESOLVED",
    )
    payment = await PaymentSpecialist(context).run(order_ids)

    trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="payment-agent",
        target="coordinator",
        decision_code="SPECIALIST_RESULTS_READY",
    )
    decision = await LLMCoordinator().run(case, entity, shipment, payment)
    trace.emit(
        case_id=context.case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="VERIFY_DECISION",
    )
    return Verifier(context).run(entity, shipment, payment, decision)
