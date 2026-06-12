"""Step 5: 工具闭环 + Pydantic 校验执行"""


from typing import Any
from openai import OpenAI
from messages import LLMResponse, ToolCall

class LLMClient:
    def __init__(self):
        self.client = OpenAI(
            base_url="http://60.13.232.228:3553/v1",
            api_key="EMPTY",
        )

    def chat(self, messages, model, tools=None) -> LLMResponse:
        response = self.completions_create(model, messages, tools)
        return self._parse_response(response)

    def completions_create(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """
        只负责发送请求，解析返回，不修改messages
        """
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None,
        )
        return response.choices[0].message


    def _parse_response(self, response: Any) -> dict[str, Any]:
        """
        把 SDK 返回的 response 转成统一格式，并写入 messages。
        """
        if response.tool_calls:
            return LLMResponse(
                type="tool_calls", 
                calls=[
                    ToolCall(
                        id=call.id,
                        name=call.function.name,
                        arguments=call.function.arguments,
                    )
                    for call in response.tool_calls
                ]
            )

        return LLMResponse(type="text", content=response.content)