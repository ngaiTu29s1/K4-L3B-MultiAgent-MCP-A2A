from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


@dataclass
class InvestigationContext:
    case_id: str
    case_data: dict[str, Any]
    gateway: EvidenceGateway
    trace: TraceWriter
    evidence_refs: set[str] = field(default_factory=set)
    _cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = field(
        default_factory=dict
    )

    async def call(self, tool_name: str, *, actor: str, **arguments: str) -> dict[str, Any]:
        key = (tool_name, tuple(sorted(arguments.items())))
        if key not in self._cache:
            self._cache[key] = await self.gateway.call(
                tool_name, case_id=self.case_id, **arguments
            )
        evidence = self._cache[key]
        evidence_ref = evidence["evidence_ref"]
        self.evidence_refs.add(evidence_ref)
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            target="coordinator",
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
        return evidence
