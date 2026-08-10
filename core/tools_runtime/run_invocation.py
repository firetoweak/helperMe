from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.tool_registry import ToolSpec
from core.tools_runtime.run_evidence import RunEvidence


class RunCapability(Protocol):
    def include_base_tools(self) -> bool:
        ...

    def evidence_roots(self) -> tuple[str, ...]:
        ...

    def runtime_instructions(self) -> list[str]:
        ...

    def tool_specs(self) -> list[ToolSpec]:
        ...

    def check_final_candidate(self, evidence: RunEvidence) -> str | None:
        ...

    def checkpoint_data(self) -> dict | None:
        ...


@dataclass(frozen=True)
class RunInvocation:
    capabilities: tuple[RunCapability, ...] = ()
