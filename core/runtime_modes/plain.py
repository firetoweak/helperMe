from __future__ import annotations

from core.messages import Conversation
from core.tools_runtime.tools_state import ToolStep, ToolsState


class PlainMode:
    def start(self, user_message: str, conversation: Conversation, llm_client, model: str) -> None:
        return None

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        return messages

    def on_assistant_text(self, conversation: Conversation) -> bool:
        return False

    def after_tool_batch(
        self,
        conversation: Conversation,
        tools_state: ToolsState,
        batch_steps: list[ToolStep],
    ) -> None:
        if any(step.ok is False for step in batch_steps):
            conversation.add_user(
                "刚才有工具调用失败。请根据工具返回的 code/error/hint 调整下一步。"
            )

    def checkpoint_data(self) -> dict | None:
        return None
