from __future__ import annotations

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


class SerialToolBatchExecutor:
    """按模型声明顺序执行并提交一批工具调用。"""

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

        for call in calls:
            if runtime_mode.handles_tool(call.name):
                tool_result = await runtime_mode.execute_tool(
                    mode_state,
                    call.name,
                    call.arguments,
                )
            else:
                tool_result = await tools_executor.execute(
                    call.name,
                    call.arguments,
                )
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
