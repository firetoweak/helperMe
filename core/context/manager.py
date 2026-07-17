from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from core.messages import ConversationMessage
from core.tools_runtime.tools_protocol import validate_tool_message_chain
from core.context.micro_compactor import MicroCompactor
from core.context.state import ContextState


@dataclass(frozen=True)
class ContextRequest:
    conversation_records: list[ConversationMessage]
    runtime_instructions: list[str]
    context_state: ContextState = field(default_factory=ContextState)


@dataclass(frozen=True)
class ModelContext:
    messages: list[dict[str, Any]]


class ContextManager:
    def __init__(
        self,
        max_tool_result_chars: int = 16_000,
        micro_compactor: MicroCompactor | None = None,
    ) -> None:
        if max_tool_result_chars <= 0:
            raise ValueError("max_tool_result_chars 必须大于 0")
        self.max_tool_result_chars = max_tool_result_chars
        self.micro_compactor = micro_compactor or MicroCompactor()

    def build(self, request: ContextRequest) -> ModelContext:
        records = request.conversation_records
        state = request.context_state
        summary_boundary_index = None
        active_records = records

        if state.summary is None:
            handoff = None
        else:
            boundary_id = state.summarized_through_message_id
            summary_boundary_index = self._find_boundary_index(
                records,
                boundary_id,
                "摘要",
            )
            if records[summary_boundary_index].payload.get("role") == "system":
                raise ValueError("压缩边界不能指向 system 消息")
            if records[0].payload.get("role") != "system":
                raise ValueError("conversation_records 的第一个消息必须是 system 角色")
            handoff = {
                "role": "assistant",
                "content": f"工作交接摘要：\n{state.summary}",
            }
            active_records = [
                records[0],
                *records[summary_boundary_index + 1 :],
            ]

        # Level 1：按 tool_artifacts 脱水；摘要前缀外的 active_records 携带原 message_id
        messages = self.micro_compactor.dehydrate(
            active_records,
            state.tool_artifacts,
        )

        if handoff is not None:
            messages.insert(1, handoff)

        if request.runtime_instructions:
            first_message = messages[0]
            if first_message.get("role") != "system":
                raise ValueError("conversation_records 的第一个消息必须是 system 角色")
            system_content = first_message.get("content")
            instruction_block = "\n\n运行时指令：\n" + "\n".join(
                instruction.strip()
                for instruction in request.runtime_instructions
            )
            first_message["content"] = system_content + instruction_block

        for message in messages:
            if (
                message.get("role") == "tool"
                and len(message["content"]) > self.max_tool_result_chars
            ):
                raise ValueError(
                    "tool message 超过单次结果字符上限: "
                    f"{len(message['content'])} > "
                    f"{self.max_tool_result_chars}"
                )

        validation = validate_tool_message_chain(messages)
        if not validation.ok:
            raise ValueError(f"工具消息链不合法: {validation.errors}")

        return ModelContext(messages=messages)

    @staticmethod
    def _find_boundary_index(
        records: list[ConversationMessage],
        message_id: str,
        boundary_name: str,
    ) -> int:
        index = next(
            (
                index
                for index, record in enumerate(records)
                if record.message_id == message_id
            ),
            None,
        )
        if index is None:
            raise ValueError(f"{boundary_name} 压缩边界不存在: {message_id}")
        return index
