from __future__ import annotations

from typing import Any
from dataclasses import dataclass
from copy import deepcopy

from core.tools_runtime.tools_protocol import validate_tool_message_chain

@dataclass(frozen=True)
class ContextRequest:
    conversation_messages: list[dict[str, Any]]
    runtime_instructions: list[str]


@dataclass(frozen=True)
class ModelContext:
    messages: list[dict[str, Any]]


class ContextManager:
    def build(self, request: ContextRequest) -> ModelContext:  

        messages = deepcopy(request.conversation_messages)
        if request.runtime_instructions:
            first_message = messages[0]
            if first_message.get("role") != "system":
                raise ValueError("conversation_messages 的第一个消息必须是 system 角色")
            system_content = first_message.get("content")
            instruction_block = "\n\n运行时指令：\n" + "\n".join(
                instruction.strip()
                for instruction in request.runtime_instructions
            )
            first_message["content"] = system_content + instruction_block
        
        validation = validate_tool_message_chain(messages)
        if not validation.ok:
            raise ValueError(f"工具消息链不合法: {validation.errors}")
        
        return ModelContext(messages=messages)