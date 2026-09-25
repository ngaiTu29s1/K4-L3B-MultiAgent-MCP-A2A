"""Entity and customer specialist for the L3B A2A workflow.

This module deliberately accepts already-extracted case clues.  The coordinator
will map the released input format to these arguments during the batch-run
phase, keeping this specialist independent of an unreleased input bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ENTITY_ACTOR = "entity-customer-agent"
COORDINATOR_ACTOR = "coordinator"
MAX_TRACE_EVIDENCE_REFS = 20

ResolutionStatus = Literal["resolved", "ambiguous", "not_found"]


@dataclass
class CaseEvidenceCache:
    """Deduplicate identical evidence lookups inside one case only.

    The cache has an explicit `case_id` and rejects cross-case use.  Cached
    evidence is the original MCP envelope, including its unmodified
    `evidence_ref`; consumers must still emit `tool_result_consumed` whenever
    they use that evidence.
    """

    case_id: str
    _responses: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = field(
        default_factory=dict
    )

    async def call(
        self,
        gateway: EvidenceGateway,
        tool_name: str,
        *,
        case_id: str,
        **arguments: str,
    ) -> dict[str, Any]:
        if case_id != self.case_id:
            raise ValueError("evidence cache cannot be shared across cases")
        key = (tool_name, tuple(sorted(arguments.items())))
        if key not in self._responses:
            self._responses[key] = await gateway.call(
                tool_name, case_id=case_id, **arguments
            )
        return self._responses[key]


@dataclass(frozen=True)
class EntityCustomerHandoff:
    """Case-scoped facts passed from Entity/Customer to the coordinator."""

    case_id: str
    status: ResolutionStatus
    resolved_order_ids: tuple[str, ...]
    rejected_candidates: tuple[str, ...]
    customer_unique_id: str | None
    related_order_ids: tuple[str, ...]
    confidence: float
    evidence_refs: tuple[str, ...]
    decision_code: str

    def as_dict(self) -> dict[str, Any]:
        """Return the internal A2A envelope payload without changing refs."""
        return {
            "case_id": self.case_id,
            "source_agent": ENTITY_ACTOR,
            "target_agent": COORDINATOR_ACTOR,
            "task_type": "resolve_entity_customer",
            "status": self.status,
            "decision_code": self.decision_code,
            "entity_ids": {
                "order_ids": list(self.resolved_order_ids),
                "customer_unique_id": self.customer_unique_id,
                "related_order_ids": list(self.related_order_ids),
            },
            "rejected_candidates": list(self.rejected_candidates),
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }


def _unique_nonempty(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def _order_evidence_match(candidate_order_id: str, data: Any) -> bool | None:
    """Return match, mismatch, or inconclusive for an order evidence response.

    The released MCP order envelope is authoritative.  If it does not expose an
    order identifier at a known direct location, the candidate remains
    unresolved rather than being rejected or assumed to match.
    """
    if not isinstance(data, dict):
        return None
    evidence_order_id = data.get("order_id", data.get("id"))
    if not isinstance(evidence_order_id, str):
        return None
    return evidence_order_id == candidate_order_id


def _related_order_ids(data: Any) -> tuple[str, ...]:
    """Read only explicit order-ID lists from customer evidence."""
    if not isinstance(data, dict):
        return ()
    values = data.get("related_order_ids", data.get("order_ids", []))
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return ()
    return _unique_nonempty(values)


async def resolve_entity_customer(
    *,
    case_id: str,
    candidate_order_ids: list[str] | tuple[str, ...],
    customer_unique_id: str | None,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    evidence_cache: CaseEvidenceCache | None = None,
) -> EntityCustomerHandoff:
    """Resolve supplied order candidates and load customer context.

    Each successful MCP response is consumed immediately in an observable trace
    event.  A failed or unrecognizable lookup does not become a fabricated ID or
    evidence reference.  This function intentionally does not retry broad
    searches; retry policy belongs to the coordinator's narrow call wrapper.
    """
    if evidence_cache is not None and evidence_cache.case_id != case_id:
        raise ValueError("entity agent received an evidence cache for another case")
    cache = evidence_cache or CaseEvidenceCache(case_id)
    customer_id = customer_unique_id.strip() if customer_unique_id else None
    candidates = _unique_nonempty(candidate_order_ids)
    order_lookup_limit = MAX_TRACE_EVIDENCE_REFS - int(customer_id is not None)
    candidates_to_query = candidates[:order_lookup_limit]
    budget_limited = len(candidates_to_query) != len(candidates)
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor=COORDINATOR_ACTOR,
        target=ENTITY_ACTOR,
        attributes={"candidate_count": len(candidates_to_query)},
    )

    evidence_refs: list[str] = []
    resolved: list[str] = []
    rejected: list[str] = []
    lookup_failed = budget_limited

    for order_id in candidates_to_query:
        try:
            evidence = await cache.call(
                gateway, "get_order", case_id=case_id, order_id=order_id
            )
        except RuntimeError:
            lookup_failed = True
            continue
        evidence_ref = evidence["evidence_ref"]
        evidence_refs.append(evidence_ref)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=ENTITY_ACTOR,
            tool_name="get_order",
            evidence_refs=[evidence_ref],
        )
        match = _order_evidence_match(order_id, evidence["data"])
        if match is True:
            resolved.append(order_id)
        elif match is False:
            rejected.append(order_id)
        else:
            lookup_failed = True

    related_orders: tuple[str, ...] = ()
    if customer_id:
        try:
            evidence = await cache.call(
                gateway,
                "get_customer_history",
                case_id=case_id,
                customer_unique_id=customer_id,
            )
        except RuntimeError:
            lookup_failed = True
        else:
            evidence_ref = evidence["evidence_ref"]
            evidence_refs.append(evidence_ref)
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=ENTITY_ACTOR,
                tool_name="get_customer_history",
                evidence_refs=[evidence_ref],
            )
            related_orders = _related_order_ids(evidence["data"])

    if resolved:
        status: ResolutionStatus = "resolved"
        confidence = 0.95
        decision_code = "ENTITY_CONFIRMED"
    elif lookup_failed:
        status = "ambiguous"
        confidence = 0.2
        decision_code = "MCP_UNAVAILABLE"
    elif candidates:
        status = "not_found"
        confidence = 0.9
        decision_code = "ENTITY_NOT_FOUND"
    else:
        status = "ambiguous"
        confidence = 0.0
        decision_code = "ENTITY_AMBIGUOUS"

    handoff = EntityCustomerHandoff(
        case_id=case_id,
        status=status,
        resolved_order_ids=_unique_nonempty(resolved),
        rejected_candidates=_unique_nonempty(rejected),
        customer_unique_id=customer_id,
        related_order_ids=related_orders,
        confidence=confidence,
        evidence_refs=_unique_nonempty(evidence_refs),
        decision_code=decision_code,
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=ENTITY_ACTOR,
        target=COORDINATOR_ACTOR,
        decision_code=handoff.decision_code,
        evidence_refs=list(handoff.evidence_refs),
    )
    return handoff
