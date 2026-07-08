from __future__ import annotations

from typing import Protocol

from core.messages import Conversation
from core.tools_runtime.state import ToolStep, ToolsState


class RuntimeMode(Protocol):
    def start(self, user_message: str, conversation: Conversation, llm_client, model: str) -> None:
        ...

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
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
