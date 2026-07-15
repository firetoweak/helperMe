from __future__ import annotations

import json
from math import ceil
from typing import Any, Protocol

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


class TemplateTokenEstimator:
    """用统一请求模板估算，并以模型真实 usage 更新系数。"""

    def __init__(self, initial_coefficient: float = 1.0) -> None:
        self._coefficient = initial_coefficient

    @property
    def coefficient(self) -> float:
        return self._coefficient

    def estimate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
    ) -> int:
        raw_size = self._raw_size(model_context, tools)
        return ceil(raw_size * self._coefficient)

    def calibrate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
        actual_input_tokens: int,
    ) -> None:
        raw_size = self._raw_size(model_context, tools)
        observed_coefficient = actual_input_tokens / raw_size
        self._coefficient = max(
            self._coefficient,
            observed_coefficient,
        )

    @staticmethod
    def _raw_size(
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
        return len(serialized)
