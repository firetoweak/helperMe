"""LiteLLM Router 到 HelperMe 窄协议的进程内适配。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import litellm

from helperme.llm.api import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from helperme.llm.config import ModelConfig
from helperme.llm.images import encode_images
from helperme.llm.types import (
    InvalidLLMResponse,
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    ToolCall,
)


_NORMALIZED_MESSAGE_FIELDS = frozenset({"role", "content", "tool_calls"})
_CONTEXT_LIMIT_ERROR_MARKERS = (
    "context length",
    "maximum context",
    "max context",
    "context window",
    "token limit",
    "tokens exceed",
    "too many tokens",
    "input is too long",
)


def _is_context_limit_error(error: str) -> bool:
    text = error.lower()
    return any(marker in text for marker in _CONTEXT_LIMIT_ERROR_MARKERS)


class LiteLLMAdapter:
    def __init__(self, config: ModelConfig):
        self._router = litellm.Router(**deepcopy(config.router))
        self._read_attachment = None

    def bind_attachment_reader(self, read) -> None:
        self._read_attachment = read

    async def __aenter__(self) -> "LiteLLMAdapter":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await litellm.close_litellm_async_clients()

    async def chat(self, messages, model, tools=None) -> LLMCallResult:
        try:
            completion = await self._completion(model, messages, tools)
        except litellm.ContextWindowExceededError as exc:
            raise LLMContextLengthError(str(exc)) from exc
        except (litellm.AuthenticationError, litellm.PermissionDeniedError) as exc:
            raise LLMAuthenticationError(str(exc)) from exc
        except (
            litellm.APIConnectionError,
            litellm.Timeout,
            litellm.RateLimitError,
            litellm.InternalServerError,
            litellm.ServiceUnavailableError,
        ) as exc:
            raise LLMTransientError(str(exc)) from exc
        except litellm.APIError as exc:
            error = str(exc)
            if _is_context_limit_error(error):
                raise LLMContextLengthError(error) from exc
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
            input_tokens = usage.prompt_tokens
            output_tokens = usage.completion_tokens
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response choice or usage fields are invalid",
            ) from exc
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = (
            getattr(usage, "cache_read_input_tokens", None)
            or (None if details is None else getattr(details, "cached_tokens", None))
            or 0
        )
        return LLMCallResult(
            response=self._parse_response(message),
            usage=LLMUsage(input_tokens, output_tokens, cached_tokens),
        )

    async def _completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> Any:
        return await self._router.acompletion(
            model=model,
            messages=encode_images(messages, self._read_attachment),
            tools=tools,
            tool_choice="auto" if tools else None,
        )

    def _parse_response(self, message: Any) -> LLMResponse:
        try:
            data = message.model_dump(exclude_none=True)
        except AttributeError as exc:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response message is invalid",
            ) from exc
        if type(data) is not dict:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response message must be an object",
            )
        raw_content = data.get("content")
        if raw_content is None:
            content = ""
        elif type(raw_content) is str:
            content = raw_content
        else:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response content must be str|null",
            )
        raw_calls = data.get("tool_calls")
        if raw_calls is None:
            calls = ()
        elif type(raw_calls) is list:
            try:
                calls = tuple(
                    ToolCall(
                        id=call["id"],
                        name=call["function"]["name"],
                        arguments=call["function"]["arguments"],
                    )
                    for call in raw_calls
                )
            except (KeyError, TypeError) as exc:
                raise InvalidLLMResponse(
                    "invalid_llm_response",
                    "model tool call fields are invalid",
                ) from exc
        else:
            raise InvalidLLMResponse(
                "invalid_llm_response",
                "model response tool_calls must be array|null",
            )
        extensions = {
            key: value
            for key, value in data.items()
            if key not in _NORMALIZED_MESSAGE_FIELDS and value is not None
        }
        return LLMResponse(content, calls, extensions)
