from __future__ import annotations

from core.model_call.types import LLMResponse
from core.tools_runtime.tools_state import ToolStep


class PlainMode:
    def create_state(self) -> None:
        return None

    def start(self, state: None) -> str | None:
        return None

    async def accept_start_response(
        self,
        state: None,
        response: LLMResponse,
    ) -> dict | None:
        raise AssertionError("plain mode has no start model call")

    def runtime_instructions(self, state: None) -> list[str]:
        return []

    def check_final_candidate(self, state: None) -> str | None:
        return None

    def on_turn_completed(self, state: None) -> None:
        pass

    def after_tool_batch(
        self,
        state: None,
        batch_steps: list[ToolStep],
    ) -> str | None:
        if any(step.ok is False for step in batch_steps):
            return (
                "刚才有工具调用失败。失败只证明本次动作未完成，不代表目标不存在或不可恢复。"
                "请根据 code/error/hint/recoverable/next_action 获取缺失状态并改用恢复动作；"
                "不要在条件未变化时原样重试，也不要直接扩大为全局结论。"
            )
        return None

    def runtime_tools(self, state: None) -> list[dict]:
        return []

    def handles_tool(self, name: str) -> bool:
        return False

    async def execute_tool(
        self,
        state: None,
        name: str,
        arguments: str,
    ) -> dict:
        raise AssertionError("plain mode has no runtime tools")

    def checkpoint_data(self, state: None) -> dict | None:
        return None
