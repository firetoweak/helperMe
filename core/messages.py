# 定义消息结构
# 管理 messages 列表的增删改
# messages.py
# ├── 类型定义     → LLMResponse、ToolCall（给 agent 做判断用）
# └── Conversation → 管理发给 API 的 messages 列表

from typing import Any

from dataclasses import dataclass

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str          # 模型返回的是 JSON 字符串，先别 parse

@dataclass
class LLMResponse:
    type: str               # "text" 或 "tool_calls"
    content: str = ""       # type=="text" 时有值
    calls: list[ToolCall] | None = None   # type=="tool_calls" 时有值


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

    def add_assistant(self, LLMResponse: LLMResponse) -> None:
        if LLMResponse.type == "text":
            self.messages.append({"role": "assistant", "content": LLMResponse.content})
        elif LLMResponse.type == "tool_calls":
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
                    for call in LLMResponse.calls
                ]
            })
        else:
            raise ValueError(f"Unknown type: {LLMResponse.type}")

