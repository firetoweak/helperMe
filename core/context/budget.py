from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.context.estimator import TokenEstimator
from core.context.manager import ModelContext


@dataclass(frozen=True)
class ModelBudgetConfig:
    context_limit: int
    input_ratio: float

    def __post_init__(self) -> None:
        if self.context_limit <= 0:
            raise ValueError("context_limit 必须大于 0")
        if not 0 < self.input_ratio < 1:
            raise ValueError("input_ratio 必须在 0 和 1 之间")


@dataclass(frozen=True)
class BudgetAssessment:
    estimated_input_tokens: int
    input_budget_tokens: int

    @property
    def allowed(self) -> bool:
        return self.estimated_input_tokens <= self.input_budget_tokens

    @property
    def overflow_tokens(self) -> int:
        return max(
            0,
            self.estimated_input_tokens - self.input_budget_tokens,
        )


class ContextBudget:
    def __init__(
        self,
        estimator: TokenEstimator,
        config: ModelBudgetConfig,
    ) -> None:
        self.estimator = estimator
        self.config = config

    def assess(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
    ) -> BudgetAssessment:
        return BudgetAssessment(
            estimated_input_tokens=self.estimator.estimate(
                model_context,
                tools,
            ),
            input_budget_tokens=int(
                self.config.context_limit * self.config.input_ratio
            ),
        )

    def observe_actual_usage(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
        actual_input_tokens: int,
    ) -> None:
        self.estimator.calibrate(
            model_context,
            tools,
            actual_input_tokens,
        )
