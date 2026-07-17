from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.context import (
    ContextComposition,
    ContextPreparationService,
    ContextState,
    MicroCompactionTrace,
    SummaryCompaction,
)
from core.messages import Conversation
from core.model_call.service import ModelCallBlocked, ModelCallService
from core.model_call.types import LLMUsage
from core.tools_runtime.tools_state import ToolStep, ToolsState


@dataclass(frozen=True)
class RuntimeModeStartResult:
    context_state: ContextState
    usage: LLMUsage | None = None
    blocked: ModelCallBlocked | None = None
    summary_compaction: SummaryCompaction | None = None
    composition: ContextComposition | None = None
    micro_compaction_trace: MicroCompactionTrace | None = None


class RuntimeMode(Protocol):
    def start(
        self,
        conversation: Conversation,
        model_calls: ModelCallService,
        model: str,
        context_preparation: ContextPreparationService,
        context_state: ContextState,
        level2_boundary_message_id: str | None,
    ) -> RuntimeModeStartResult:
        ...

    def runtime_instructions(self) -> list[str]:
        ...

    def on_assistant_text(self, conversation: Conversation) -> bool:
        ...

    def after_tool_batch(
        self,
        conversation: Conversation,
        tools_state: ToolsState,
        batch_steps: list[ToolStep],
    ) -> None:
        ...

    def checkpoint_data(self) -> dict | None:
        ...
