from __future__ import annotations

from core.messages import Conversation
from core.model_call.types import LLMResponse
from core.tools_runtime.tools_state import ToolStep, ToolsState


class PlainMode:
    def start(self, conversation: Conversation) -> str | None:
        return None

    def accept_start_response(self, response: LLMResponse) -> dict | None:
        raise AssertionError("plain mode has no start model call")

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
        pass

    def handle_tool_failures(
        self,
        conversation: Conversation,
        failed_steps: list[ToolStep],
    ) -> str | None:
        conversation.add_user(
            "刚才有工具调用失败。请根据工具返回的 code/error/hint 调整下一步。"
        )
        return None

    def accept_tool_failure_response(
        self,
        response: LLMResponse,
    ) -> dict | None:
        raise AssertionError("plain mode has no replanning model call")

    def checkpoint_data(self) -> dict | None:
        return None
