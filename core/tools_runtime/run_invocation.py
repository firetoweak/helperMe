from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from core.tool_registry import ToolSpec
from core.tools_runtime.run_evidence import RunEvidence

if TYPE_CHECKING:
    from core.runtime_modes import RuntimeMode
    from core.tools_runtime.progressive_toolsets import ToolsetProvider


class RunCapability(Protocol):
    def base_tool_names(self) -> tuple[str, ...] | None:
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
    toolset_provider: ToolsetProvider | None = None
    runtime_mode: RuntimeMode | None = None
