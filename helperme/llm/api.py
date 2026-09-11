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
    """Assistant 使用的最小模型调用协议。

    content 为文本或有序内容块；图片块为
    {"type": "image", "id": 内容寻址 id, "mime": MIME 类型}。
    Client 在请求边界读取字节，不修改上层消息或持久化 base64。
    """

    async def chat(
        self,
        messages: list[dict[str, object]],
        model: str,
        tools: list[dict[str, object]] | None = None,
    ) -> LLMCallResult:
        ...
