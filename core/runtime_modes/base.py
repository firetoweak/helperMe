from __future__ import annotations

from typing import Protocol

from core.messages import Conversation
from core.model_call.types import LLMResponse
from core.tools_runtime.tools_state import ToolStep, ToolsState

class RuntimeMode(Protocol):
    def start(self, conversation: Conversation) -> str | None:
        ...

    def accept_start_response(self, response: LLMResponse) -> dict | None:
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

    def handle_tool_failures(
        self,
        conversation: Conversation,
        failed_steps: list[ToolStep],
    ) -> str | None:
        ...

    def accept_tool_failure_response(
        self,
        response: LLMResponse,
    ) -> dict | None:
        ...

    def checkpoint_data(self) -> dict | None:
        ...
