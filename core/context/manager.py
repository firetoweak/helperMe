from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from core.messages import ConversationMessage
from core.context.micro_compactor import MicroCompactor
from core.context.state import ContextState


@dataclass(frozen=True)
class ContextRequest:
    conversation_records: list[ConversationMessage]
    runtime_instructions: list[str]
    context_state: ContextState = field(default_factory=ContextState)
    contextual_user_fragments: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelContext:
    messages: list[dict[str, Any]]


class ContextManager:
    def __init__(
        self,
        micro_compactor: MicroCompactor | None = None,
    ) -> None:
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
            )
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
            system_content = first_message["content"]
            instruction_block = "\n\n运行时指令：\n" + "\n".join(
                instruction.strip()
                for instruction in request.runtime_instructions
            )
            first_message["content"] = system_content + instruction_block

        if request.contextual_user_fragments:
            messages.insert(1, {
                "role": "user",
                "content": "\n\n".join(
                    fragment.strip()
                    for fragment in request.contextual_user_fragments
                ),
            })

        return ModelContext(messages=messages)

    @staticmethod
    def _find_boundary_index(
        records: list[ConversationMessage],
        message_id: str,
    ) -> int:
        return next(
            index
            for index, record in enumerate(records)
            if record.message_id == message_id
        )
