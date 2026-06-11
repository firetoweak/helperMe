"""Step 5: 工具闭环 + Pydantic 校验执行"""

import json
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from tools import TOOL_SPECS, TOOLS

client = OpenAI(
    base_url="http://60.13.232.228:3553/v1",
    api_key="EMPTY",
)

messages: list[dict[str, Any]] = []


def reset() -> None:
    """清空对话历史。"""
    global messages
    messages = []


def set_system_prompt(content: str) -> None:
    global messages
    messages = [m for m in messages if m["role"] != "system"]
    messages.insert(0, {"role": "system", "content": content})


def _parse_response(msg: Any) -> dict[str, Any]:
    """把 SDK 返回的 message 转成统一格式，并写入 messages。"""
    if msg.tool_calls:
        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": call.type,
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in msg.tool_calls
                ],
            }
        )
        return {
            "type": "tool_calls",
            "calls": [
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                }
                for call in msg.tool_calls
            ],
        }

    reply = msg.content or ""
    messages.append({"role": "assistant", "content": reply})
    return {"type": "text", "content": reply}


def _call_model(model: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS,
    )
    return _parse_response(response.choices[0].message)


def chat(user_message: str, model: str = "default") -> str:
    """普通对话，不带工具。"""
    messages.append({"role": "user", "content": user_message})
    response = client.chat.completions.create(model=model, messages=messages)
    reply = response.choices[0].message.content or ""
    messages.append({"role": "assistant", "content": reply})
    return reply


def chat_with_tools(user_message: str, model: str = "default") -> dict[str, Any]:
    """Step 3：模型决定直接回复还是调用工具。"""
    messages.append({"role": "user", "content": user_message})
    return _call_model(model)


def submit_tool_results(
    tool_results: list[dict[str, str]],
    model: str = "default",
) -> dict[str, Any]:
    """Step 4：把工具执行结果回传给模型，让它继续决策。

    tool_results 格式:
        [{"tool_call_id": "call_xxx", "content": "2026-06-11"}, ...]
    """
    for result in tool_results:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": result["tool_call_id"],
                "content": result["content"],
            }
        )
    return _call_model(model)


def execute_tool(name: str, arguments: str) -> str:
    """解析 arguments JSON → Pydantic 校验 → 调用工具函数。"""
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)

    try:
        payload = json.loads(arguments or "{}")
        data = spec.input_model.model_validate(payload)
        result = spec.handler(data)
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid json: {exc}"}, ensure_ascii=False)
    except ValidationError as exc:
        return json.dumps({"error": exc.errors()}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — 工具运行时错误需回传给模型
        return json.dumps({"error": str(exc)}, ensure_ascii=False)