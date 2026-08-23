from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from collections import deque
from typing import Protocol
import json

import tiktoken


class TokenEstimator(Protocol):
    def estimate(
        self,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]],
    ) -> int:
        ...

    def calibrate(
        self,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]],
        actual_input_tokens: int,
    ) -> None:
        ...


@dataclass(frozen=True, slots=True)
class BudgetAssessment:
    estimated_input_tokens: int
    input_budget_tokens: int

    @property
    def allowed(self) -> bool:
        return self.estimated_input_tokens <= self.input_budget_tokens


class InputBudget:
    """新栈自己的输入预算。只判断是否超限，不复用 core.context。"""

    def __init__(
        self,
        estimator: TokenEstimator,
        *,
        context_limit: int,
        input_ratio: float,
    ) -> None:
        if type(context_limit) is not int or context_limit <= 0:
            raise ValueError("context_limit 必须大于 0")
        if type(input_ratio) is not float or not 0 < input_ratio < 1:
            raise ValueError("input_ratio 必须在 0 和 1 之间")
        self.estimator = estimator
        self.context_limit = context_limit
        self.input_ratio = input_ratio

    @property
    def input_budget_tokens(self) -> int:
        return int(self.context_limit * self.input_ratio)

    def assess(
        self,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]],
    ) -> BudgetAssessment:
        return BudgetAssessment(
            estimated_input_tokens=self.estimator.estimate(messages, tools),
            input_budget_tokens=self.input_budget_tokens,
        )

    def observe_actual_usage(
        self,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]],
        actual_input_tokens: int,
    ) -> None:
        self.estimator.calibrate(messages, tools, actual_input_tokens)


class TiktokenEstimator:
    def __init__(self, window_size: int = 8) -> None:
        if window_size <= 0:
            raise ValueError("window_size 必须大于 0")
        self._encoding = tiktoken.get_encoding("o200k_base")
        self._observed_coefficients: deque[float] = deque(maxlen=window_size)

    @property
    def coefficient(self) -> float:
        return max((1.0, *self._observed_coefficients))

    def estimate(
        self,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]],
    ) -> int:
        return ceil(self._base_tokens(messages, tools) * self.coefficient)

    def calibrate(
        self,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]],
        actual_input_tokens: int,
    ) -> None:
        base = self._base_tokens(messages, tools)
        if base > 0:
            self._observed_coefficients.append(actual_input_tokens / base)

    def _base_tokens(
        self,
        messages: Sequence[dict[str, object]],
        tools: Sequence[dict[str, object]],
    ) -> int:
        serialized = json.dumps(
            {"messages": list(messages), "tools": list(tools)},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return len(self._encoding.encode_ordinary(serialized))
