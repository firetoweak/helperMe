"""外部模型 API 客户端。"""

from __future__ import annotations

from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from core.context_compactor import is_context_limit_error
from core.model_call.types import (
    InvalidLLMResponse,
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    ToolCall,
)


class LLMTransientError(RuntimeError):
    pass


class LLMContextLengthError(RuntimeError):
    pass


class LLMClient:
    def __init__(self):
        http_client = httpx.Client(
            trust_env=False,
            timeout=httpx.Timeout(
                connect=10.0,
                read=120.0,
                write=30.0,
                pool=10.0,
            ),
        )
        self.client = OpenAI(
            base_url="http://60.13.232.228:3553/v1",
            api_key="EMPTY",
            http_client=http_client,
            max_retries=0,
        )

    def chat(self, messages, model, tools=None) -> LLMCallResult:
        try:
            completion = self.completions_create(model, messages, tools)
        except OpenAIError as exc:
            error = str(exc)
            if is_context_limit_error(error):
                raise LLMContextLengthError(error) from exc
            if isinstance(
                exc,
                (APIConnectionError, APITimeoutError, RateLimitError),
            ) or (
                isinstance(exc, APIStatusError)
                and exc.status_code >= 500
            ):
                raise LLMTransientError(error) from exc
            raise

        return LLMCallResult(
            response=self._parse_response(completion.choices[0].message),
            usage=LLMUsage(
                input_tokens=completion.usage.prompt_tokens,
                output_tokens=completion.usage.completion_tokens,
            ),
        )

    def completions_create(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> Any:
        """发送一次请求并返回完整 SDK completion，不修改 messages。"""
        return self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None,
        )

    def _parse_response(self, response: Any) -> LLMResponse:
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
                ],
            )

        if isinstance(response.content, str) and response.content.strip():
            return LLMResponse(type="text", content=response.content)

        raise InvalidLLMResponse(
            "empty_model_response",
            "model response contains neither tool calls nor non-empty text",
        )
