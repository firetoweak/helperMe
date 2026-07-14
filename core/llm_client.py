"""Step 5: 工具闭环 + Pydantic 校验执行"""


from typing import Any
from openai import OpenAI
from core.messages import InvalidLLMResponse, LLMResponse, ToolCall
import httpx

class LLMClient:
    def __init__(self):
        http_client = httpx.Client(
            trust_env=False,   # 关键：不读系统代理
            timeout=httpx.Timeout(
                connect=10.0,   # 连不上尽快失败
                read=120.0,     # 模型推理可以慢，读超时保留
                write=30.0,
                pool=10.0,
            )
        )
        self.client = OpenAI(
            base_url="http://60.13.232.228:3553/v1",
            api_key="EMPTY",
            http_client=http_client,
            max_retries=0,  # retry 交给 RunRuntime，避免双层叠加
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


    def _parse_response(self, response: Any) -> LLMResponse:
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

        if isinstance(response.content, str) and response.content.strip():
            return LLMResponse(type="text", content=response.content)

        raise InvalidLLMResponse(
            "empty_model_response",
            "model response contains neither tool calls nor non-empty text",
        )
