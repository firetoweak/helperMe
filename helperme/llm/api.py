from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from helperme.llm.codec import (
    LLMRemoteError,
    decode_llm_error,
    decode_llm_result,
    encode_llm_error,
    encode_llm_result,
)
from helperme.llm.images import encode_images
from helperme.llm.types import (
    InvalidLLMResponse,
    LLMCallResult,
    LLMResponse,
    ToolCall,
)


__all__ = [
    "ContentDeltaSink",
    "InvalidLLMResponse",
    "LLMApi",
    "LLMAuthenticationError",
    "LLMCallResult",
    "LLMContextLengthError",
    "LLMProviderError",
    "LLMRemoteError",
    "LLMResponse",
    "LLMTransientError",
    "ToolCall",
    "decode_llm_error",
    "decode_llm_result",
    "encode_images",
    "encode_llm_error",
    "encode_llm_result",
]


class LLMTransientError(RuntimeError):
    pass


class LLMContextLengthError(RuntimeError):
    pass


class LLMProviderError(RuntimeError):
    pass


class LLMAuthenticationError(LLMProviderError):
    pass


ContentDeltaSink = Callable[[str], Awaitable[None] | None]


class LLMApi(Protocol):
    """Assistant 使用的最小模型调用协议。

    content 为文本或有序内容块；图片块为
    {"type": "image", "id": 内容寻址 id, "mime": MIME 类型}。
    调用方在请求边界读取附件字节并编码；共享实现不持有 Session 附件闭包。
    """

    async def chat(
        self,
        messages: list[dict[str, object]],
        model: str,
        tools: list[dict[str, object]] | None = None,
        *,
        on_content_delta: ContentDeltaSink | None = None,
    ) -> LLMCallResult:
        ...
