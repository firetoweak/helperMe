from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from core.model_call import LLMResponse


@dataclass(frozen=True)
class ConversationMessage:
    message_id: str
    payload: dict[str, Any]


class Conversation:
    def __init__(self):
        self.records: list[ConversationMessage] = []

    def set_system_prompt(self, content: str) -> None:
        if self.records:
            raise RuntimeError("system prompt 只能在空 Conversation 中设置")
        self._append({"role": "system", "content": content})

    def add_user(self, content: str) -> None:
        self._append({"role": "user", "content": content})

    def add_tools_result(self, tool_results: list[dict[str, str]]) -> None:
        for result in tool_results:
            self._append({
                "role": "tool", 
                "tool_call_id": result["tool_call_id"], 
                "content": result["content"]
            })

    def add_assistant(self, response: LLMResponse) -> None:
        if response.type == "text":
            self._append({"role": "assistant", "content": response.content})
        elif response.type == "tool_calls":
            self._append({
                "role": "assistant", 
                "content": None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    } 
                    for call in response.calls
                ]
            })
        else:
            raise ValueError(f"Unknown type: {response.type}")

    def protocol_messages(self) -> list[dict[str, Any]]:
        return [record.payload for record in self.records]

    def _append(self, payload: dict[str, Any]) -> None:
        self.records.append(
            ConversationMessage(
                message_id=uuid4().hex,
                payload=payload,
            )
        )

