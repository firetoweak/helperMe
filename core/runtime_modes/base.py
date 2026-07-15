from __future__ import annotations

from typing import Protocol

from core.context import ContextManager
from core.messages import Conversation
from core.model_call.service import ModelCallBlocked, ModelCallService
from core.model_call.types import LLMUsage
from core.tools_runtime.tools_state import ToolStep, ToolsState


class RuntimeMode(Protocol):
    def start(
        self,
        conversation: Conversation,
        model_calls: ModelCallService,
        model: str,
        context_manager: ContextManager,
    ) -> LLMUsage | ModelCallBlocked | None:
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
