from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from core.model_call import LLMResponse
from core.approval import ApprovalRequest, ApprovalResolution


@dataclass(frozen=True)
class ConversationMessage:
    message_id: str
    payload: dict[str, Any]


class Conversation:
    def __init__(self):
        self.records: list[ConversationMessage] = []
        self.approval_requests: dict[str, ApprovalRequest] = {}
        self.approval_resolutions: dict[str, ApprovalResolution] = {}

    def set_system_prompt(self, content: str) -> None:
        self._append({"role": "system", "content": content})

    def add_user(self, content: str) -> None:
        self._append({"role": "user", "content": content})

    def record_approval_request(self, request: ApprovalRequest) -> None:
        self.approval_requests[request.id] = request

    def get_approval_request(self, approval_id: str) -> ApprovalRequest:
        return self.approval_requests[approval_id]

    def record_approval_resolution(
        self,
        resolution: ApprovalResolution,
    ) -> None:
        self.approval_resolutions[resolution.approval_id] = resolution

    def add_tools_result(self, tool_results: list[dict[str, str]]) -> None:
        for result in tool_results:
            self._append({
                "role": "tool", 
                "tool_call_id": result["tool_call_id"], 
                "content": result["content"]
            })

    def add_assistant(self, response: LLMResponse) -> None:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": response.content or None,
        }
        if response.calls:
            message["tool_calls"] = [
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
        self._append(message)

    def protocol_messages(self) -> list[dict[str, Any]]:
        return [record.payload for record in self.records]

    def _append(self, payload: dict[str, Any]) -> None:
        self.records.append(
            ConversationMessage(
                message_id=uuid4().hex,
                payload=payload,
            )
        )

