from __future__ import annotations

from core.model_call.types import LLMResponse
from core.tools_runtime.tools_state import ToolStep


class PlainMode:
    def create_state(self) -> None:
        return None

    def start(self, state: None) -> str | None:
        return None

    def accept_start_response(
        self,
        state: None,
        response: LLMResponse,
    ) -> dict | None:
        raise AssertionError("plain mode has no start model call")

    def runtime_instructions(self, state: None) -> list[str]:
        return []

    def check_final_candidate(self, state: None) -> str | None:
        return None

    def on_run_completed(self, state: None) -> None:
        pass

    def after_tool_batch(
        self,
        state: None,
        batch_steps: list[ToolStep],
    ) -> str | None:
        if any(step.ok is False for step in batch_steps):
            return (
                "刚才有工具调用失败。请根据工具返回的 code/error/hint 调整下一步。"
            )
        return None

    def runtime_tools(self, state: None) -> list[dict]:
        return []

    def handles_tool(self, name: str) -> bool:
        return False

    def execute_tool(
        self,
        state: None,
        name: str,
        arguments: str,
    ) -> dict:
        raise AssertionError("plain mode has no runtime tools")

    def checkpoint_data(self, state: None) -> dict | None:
        return None
