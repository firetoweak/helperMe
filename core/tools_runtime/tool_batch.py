from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

from core.model_call.types import ToolCall
from core.runtime_artifacts import ToolResultExternalizer
from core.runtime_modes import RuntimeMode
from core.tools_runtime.run_evidence import RunEvidenceRecorder
from core.tools_runtime.tools_executor import ToolsExecutor, encode_tool_result
from core.tools_runtime.tools_protocol import build_tool_messages
from core.tools_runtime.tools_state import ToolStep, ToolsState


@dataclass(frozen=True)
class ToolBatchOutcome:
    steps: list[ToolStep]
    messages: list[dict[str, str]]
    result_chars_before: int
    result_chars_after: int
    externalized_count: int


class ConcurrentToolBatchExecutor:
    """并发执行模型同一轮声明的工具调用，并按声明顺序提交结果。"""

    def __init__(self, result_externalizer: ToolResultExternalizer) -> None:
        self.result_externalizer = result_externalizer

    async def execute(
        self,
        *,
        calls: Iterable[ToolCall],
        tools_executor: ToolsExecutor,
        runtime_mode: RuntimeMode,
        mode_state: Any,
        tools_state: ToolsState,
        evidence_recorder: RunEvidenceRecorder,
    ) -> ToolBatchOutcome:
        calls = tuple(calls)
        steps = tools_state.add_calls(calls)
        result_chars_before = 0
        result_chars_after = 0
        externalized_count = 0

        async def execute_call(call: ToolCall) -> dict[str, Any]:
            if runtime_mode.handles_tool(call.name):
                return await runtime_mode.execute_tool(
                    mode_state,
                    call.name,
                    call.arguments,
                )
            return await tools_executor.execute(
                call.name,
                call.arguments,
            )

        # 同一 Round 即模型的并行意图。gather 保持返回值与输入顺序一致；
        # 取消本批次时，取消会继续传播到所有尚未完成的工具调用。
        tool_results = await asyncio.gather(
            *(execute_call(call) for call in calls),
            return_exceptions=True,
        )

        # handler 缺陷仍是内部异常，不伪装成可恢复的 Tool Result；但必须先等
        # 同轮兄弟调用全部收束，避免 Run 已失败后仍有后台调用继续产生副作用。
        for result in tool_results:
            if isinstance(result, BaseException):
                raise result

        # 共享账本只在所有调用结束后由当前 Task 顺序提交，避免完成时序
        # 改变 Evidence、Artifact、ToolsState 和 Conversation 的事实顺序。
        for call, tool_result in zip(calls, tool_results, strict=True):
            evidence_recorder.record(
                call.id,
                call.name,
                call.arguments,
                tool_result,
            )
            outcome = self.result_externalizer.process(tool_result)
            result_chars_before += outcome.original_chars
            result_chars_after += outcome.projected_chars
            if outcome.externalized:
                externalized_count += 1
            tools_state.add_result(call.id, outcome.result)

        return ToolBatchOutcome(
            steps=steps,
            messages=build_tool_messages(steps, encode_tool_result),
            result_chars_before=result_chars_before,
            result_chars_after=result_chars_after,
            externalized_count=externalized_count,
        )
