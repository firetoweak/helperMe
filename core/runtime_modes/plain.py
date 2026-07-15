from __future__ import annotations

from core.context import ContextManager
from core.messages import Conversation
from core.model_call.service import ModelCallBlocked, ModelCallService
from core.tools_runtime.tools_state import ToolStep, ToolsState


class PlainMode:
    def start(
        self,
        conversation: Conversation,
        model_calls: ModelCallService,
        model: str,
        context_manager: ContextManager,
    ) -> ModelCallBlocked | None:
        return None

    def runtime_instructions(self) -> list[str]:
        return []

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
