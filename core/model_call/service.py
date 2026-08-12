from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.context import BudgetAssessment, ContextBudget, ModelContext
from core.model_call.types import LLMCallResult

if TYPE_CHECKING:
    from core.model_call.client import LLMClient


@dataclass(frozen=True)
class ModelCallRequest:
    context: ModelContext
    tools: list[dict[str, Any]]


@dataclass(frozen=True)
class ModelCallBlocked:
    assessment: BudgetAssessment


ModelCallOutcome = LLMCallResult | ModelCallBlocked


class ModelCallService:
    def __init__(
        self,
        llm_client: LLMClient,
        context_budget: ContextBudget,
    ) -> None:
        self.llm_client = llm_client
        self.context_budget = context_budget

    async def call(
        self,
        request: ModelCallRequest,
        model: str,
    ) -> ModelCallOutcome:
        assessment = self.context_budget.assess(
            request.context,
            request.tools,
        )
        if not assessment.allowed:
            return ModelCallBlocked(assessment)

        result = await self.llm_client.chat(
            request.context.messages,
            model,
            request.tools,
        )

        self.context_budget.observe_actual_usage(
            request.context,
            request.tools,
            result.usage.input_tokens,
        )
        return result
