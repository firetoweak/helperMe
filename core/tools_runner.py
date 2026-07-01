from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.checkpoint import (
    Checkpoint,
    budget_stop_checkpoint,
    checkpoint_to_record,
    format_checkpoint,
    message_chain_invalid_checkpoint,
    run_completed_checkpoint,
    run_started_checkpoint,
    tool_batch_completed_checkpoint,
)
from core.messages import Conversation
from core.llm_client import LLMClient
from core.tool_registry import get_tools
from core.tools_executor import encode_tool_result, execute_tool
from core.tools_state import ToolsState


@dataclass
class RunResult:
    status: str
    answer: str
    checkpoints: list[Checkpoint]
    error: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "error": self.error,
            "checkpoints": [
                checkpoint_to_record(checkpoint)
                for checkpoint in self.checkpoints
            ],
        }


class ToolsRunner:
    """最小 tool-calling 运行内核。"""

    def __init__(self, llm_client: LLMClient, model: str):
        self.llm_client = llm_client
        self.model = model
        self.checkpoints: list[Checkpoint] = []

    def run(self, conversation: Conversation, user_message: str, max_rounds: int = 10) -> RunResult:
        self.checkpoints = []
        tools_state = ToolsState()
        self.checkpoints.append(run_started_checkpoint(max_rounds))
        conversation.add_user(user_message)

        for round_index in range(1, max_rounds + 1):
            validation = tools_state.validate_messages(conversation.messages)
            if not validation.ok:
                checkpoint = message_chain_invalid_checkpoint(validation.to_dict())
                self.checkpoints.append(checkpoint)
                return RunResult(
                    status="terminated",
                    answer=format_checkpoint(checkpoint),
                    checkpoints=self.checkpoints,
                    error="message_chain_invalid",
                )

            response = self.llm_client.chat(
                conversation.messages,
                self.model,
                get_tools(),
            )

            conversation.add_assistant(response)
            if response.type == "text":
                if not response.content:
                    conversation.add_user("你刚才返回了空内容。请继续完成任务：如果需要修改就调用工具；如果已完成就给出总结。")
                    continue
                self.checkpoints.append(run_completed_checkpoint(response.content))
                return RunResult(
                    status="completed",
                    answer=response.content,
                    checkpoints=self.checkpoints,
                )

            calls = response.calls or []
            batch_steps = tools_state.add_calls(calls)

            for call in calls:
                try:
                    tool_result = execute_tool(call.name, call.arguments)
                except Exception as exc:
                    tools_state.mark_failed(
                        call.id,
                        code="TOOL_EXECUTION_CRASHED",
                        error=str(exc),
                        hint="工具执行过程异常，已补齐错误结果，避免 tool_call 链路断裂。",
                    )
                else:
                    tools_state.add_result(call.id, tool_result)

            tool_results = tools_state.to_tool_messages(encode_tool_result, batch_steps)
            conversation.add_tools_result(tool_results)
            self.checkpoints.append(
                tool_batch_completed_checkpoint(round_index, tools_state, len(calls))
            )

        if tools_state.has_pending():
            tools_state.repair_pending(
                code="MAX_ROUNDS_EXCEEDED",
                error="运行达到最大轮次，仍存在没有结果的 tool_call。",
                hint="已补齐错误结果以保持工具调用链路完整。",
            )
        checkpoint = budget_stop_checkpoint(max_rounds, tools_state)
        self.checkpoints.append(checkpoint)
        return RunResult(
            status="terminated",
            answer=format_checkpoint(checkpoint),
            checkpoints=self.checkpoints,
            error="max_rounds_exceeded",
        )

    def checkpoint_records(self) -> list[dict[str, Any]]:
        return [checkpoint_to_record(checkpoint) for checkpoint in self.checkpoints]
