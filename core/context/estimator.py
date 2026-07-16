from __future__ import annotations

import json
from collections import deque
from math import ceil
from typing import Any, Protocol

import tiktoken

from core.context.manager import ModelContext


class TokenEstimator(Protocol):
    def estimate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
    ) -> int:
        ...

    def calibrate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
        actual_input_tokens: int,
    ) -> None:
        ...


class TiktokenTokenEstimator:
    """编码完整请求模板，并用近期真实 usage 校准模型差异。"""

    def __init__(self, window_size: int = 8) -> None:
        if window_size <= 0:
            raise ValueError("window_size 必须大于 0")
        self._encoding = tiktoken.get_encoding("o200k_base")
        self._observed_coefficients: deque[float] = deque(
            maxlen=window_size
        )

    @property
    def coefficient(self) -> float:
        return max((1.0, *self._observed_coefficients))

    def estimate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
    ) -> int:
        base_tokens = self._base_tokens(model_context, tools)
        return ceil(base_tokens * self.coefficient)

    def calibrate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
        actual_input_tokens: int,
    ) -> None:
        base_tokens = self._base_tokens(model_context, tools)
        self._observed_coefficients.append(
            actual_input_tokens / base_tokens
        )

    def _base_tokens(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
    ) -> int:
        request_template = {
            "messages": model_context.messages,
            "tools": tools,
        }
        serialized = json.dumps(
            request_template,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return len(self._encoding.encode_ordinary(serialized))
