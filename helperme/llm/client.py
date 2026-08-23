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

from helperme.llm.api import (
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from helperme.llm.config import ModelConfig
from helperme.llm.types import (
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    InvalidLLMResponse,
    ToolCall,
)


CONTEXT_LIMIT_ERROR_MARKERS = (
    "context length",
    "maximum context",
    "max context",
    "context window",
    "token limit",
    "tokens exceed",
    "too many tokens",
    "input is too long",
)


def is_context_limit_error(error: str) -> bool:
    text = error.lower()
    return any(marker in text for marker in CONTEXT_LIMIT_ERROR_MARKERS)


class LLMClient:
    def __init__(self, config: ModelConfig):
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
            raise LLMProviderError(error) from exc

        try:
            choices = completion.choices
            usage = completion.usage
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response is missing choices or usage",
            ) from exc
        if type(choices) is not list or not choices or usage is None:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response is missing choices or usage",
            )
        try:
            message = choices[0].message
            prompt_tokens = usage.prompt_tokens
            completion_tokens = usage.completion_tokens
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response choice or usage fields are invalid",
            ) from exc
        return LLMCallResult(
            response=self._parse_response(message),
            usage=LLMUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
            ),
        )

    async def completions_create(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> Any:
        return await self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None,
        )

    def _parse_response(self, response: Any) -> LLMResponse:
        try:
            raw_content = response.content
            raw_calls = response.tool_calls
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response message fields are invalid",
            ) from exc
        if raw_content is None:
            content = ""
        elif type(raw_content) is str:
            content = raw_content
        else:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response content must be str|null",
            )
        if raw_calls is None:
            calls = ()
        elif type(raw_calls) is list:
            try:
                calls = tuple(
                    ToolCall(
                        id=call.id,
                        name=call.function.name,
                        arguments=call.function.arguments,
                    )
                    for call in raw_calls
                )
            except AttributeError as exc:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model tool call fields are invalid",
                ) from exc
        else:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response tool_calls must be array|null",
            )
        return LLMResponse(
            content=content,
            calls=calls,
        )
