from __future__ import annotations

from helperme.llm.api import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)


def assistant_failure_message(error: BaseException) -> str | None:
    if isinstance(error, LLMAuthenticationError):
        return (
            "模型认证失败：API 密钥无效，或当前密钥无权访问配置的模型。"
            "请检查 config.json 中的 model.api_key 和 model.name。"
        )
    if isinstance(error, LLMTransientError):
        return f"模型服务暂时不可用，自动重试仍未成功：{error}"
    if isinstance(error, LLMContextLengthError):
        return f"模型输入超出上下文限制：{error}"
    if isinstance(error, LLMProviderError):
        return f"模型请求失败：{error}"
    return None
