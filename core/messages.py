from typing import Any

from core.model_call import LLMResponse


# === 对话状态管理 ===

class Conversation:
    def __init__(self):
        self.messages: list[dict[str, Any]] = []

    
    def reset(self) -> None:
        self.messages = []
    
    def set_system_prompt(self, content: str) -> None:
        self.messages = [m for m in self.messages if m["role"] != "system"]
        self.messages.insert(0, {"role": "system", "content": content})

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_tools_result(self, tool_results: list[dict[str, str]]) -> None:
        for result in tool_results:
            self.messages.append({
                "role": "tool", 
                "tool_call_id": result["tool_call_id"], 
                "content": result["content"]
            })

    def add_assistant(self, response: LLMResponse) -> None:
        if response.type == "text":
            self.messages.append({"role": "assistant", "content": response.content})
        elif response.type == "tool_calls":
            self.messages.append({
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

