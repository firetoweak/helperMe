"""外部模型 API 客户端。"""

from __future__ import annotations

from typing import Any

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)

from core.context.compactor import is_context_limit_error
from core.model_call.config import ModelConfig, load_model_config
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
    def __init__(self, config: ModelConfig | None = None):
        config = config or load_model_config()
        http_client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(
                connect=10.0,
                read=300.0,
                write=30.0,
                pool=10.0,
            ),
        )
        self.client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            http_client=http_client,
            max_retries=0,
        )

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.close()

    async def chat(self, messages, model, tools=None) -> LLMCallResult:
        try:
            completion = await self.completions_create(model, messages, tools)
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

    async def completions_create(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> Any:
        """发送一次请求并返回完整 SDK completion，不修改 messages。"""
        return await self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None,
        )

    def _parse_response(self, response: Any) -> LLMResponse:
        content = response.content if isinstance(response.content, str) else ""
        return LLMResponse(
            content=content,
            calls=tuple(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=call.function.arguments,
                )
                for call in response.tool_calls or ()
            ),
        )
