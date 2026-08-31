from __future__ import annotations

from typing import Protocol

from helperme.llm.types import (
    InvalidLLMResponse,
    LLMCallResult,
    LLMResponse,
    ToolCall,
)


__all__ = [
    "InvalidLLMResponse",
    "LLMApi",
    "LLMAuthenticationError",
    "LLMCallResult",
    "LLMContextLengthError",
    "LLMProviderError",
    "LLMResponse",
    "LLMTransientError",
    "ToolCall",
]


class LLMTransientError(RuntimeError):
    pass


class LLMContextLengthError(RuntimeError):
    pass


class LLMProviderError(RuntimeError):
    pass


class LLMAuthenticationError(LLMProviderError):
    pass


class LLMApi(Protocol):
    """Assistant 使用的最小模型调用协议。"""

    async def chat(
        self,
        messages: list[dict[str, object]],
        model: str,
        tools: list[dict[str, object]] | None = None,
    ) -> LLMCallResult:
        ...
