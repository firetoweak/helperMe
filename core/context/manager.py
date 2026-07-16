from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from core.messages import ConversationMessage
from core.tools_runtime.tools_protocol import validate_tool_message_chain
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
    def __init__(self, max_tool_result_chars: int = 16_000) -> None:
        if max_tool_result_chars <= 0:
            raise ValueError("max_tool_result_chars 必须大于 0")
        self.max_tool_result_chars = max_tool_result_chars

    def build(self, request: ContextRequest) -> ModelContext:
        records = request.conversation_records
        state = request.context_state
        if state.summary is None:
            messages = deepcopy([r.payload for r in records])
        else:
            boundary_id = state.compacted_through_message_id
            boundary_index = next(
                (i for i, r in enumerate(records) if r.message_id == boundary_id),
                None,
            )
            if boundary_index is None:
                raise ValueError(
                    f"压缩边界不存在: {boundary_id}"
                )
            if records[boundary_index].payload.get("role") == "system":
                raise ValueError("压缩边界不能指向 system 消息")
            # 保留 system（通常是 records[0]），再拼 handoff + 后缀
            system_payload = deepcopy(records[0].payload)
            if system_payload.get("role") != "system":
                raise ValueError("conversation_records 的第一个消息必须是 system 角色")
            handoff = {
                "role": "assistant",
                "content": f"工作交接摘要：\n{state.summary}",
            }
            suffix = deepcopy(
                [r.payload for r in records[boundary_index + 1 :]]
            )
            messages = [system_payload, handoff, *suffix]

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
