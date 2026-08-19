from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from core.tool_registry import ToolSpec
from core.tools_runtime.turn_evidence import TurnEvidence

if TYPE_CHECKING:
    from core.runtime_modes import RuntimeMode
    from core.tools_runtime.progressive_toolsets import ToolsetProvider


class TurnCapability(Protocol):
    def base_tool_names(self) -> tuple[str, ...] | None:
        ...

    def evidence_roots(self) -> tuple[str, ...]:
        ...

    def runtime_instructions(self) -> list[str]:
        ...

    def tool_specs(self) -> list[ToolSpec]:
        ...

    def check_final_candidate(self, evidence: TurnEvidence) -> str | None:
        ...

    def checkpoint_data(self) -> dict | None:
        ...


@dataclass(frozen=True)
class TurnInvocation:
    capabilities: tuple[TurnCapability, ...] = ()
    toolset_provider: ToolsetProvider | None = None
    runtime_mode: RuntimeMode | None = None
