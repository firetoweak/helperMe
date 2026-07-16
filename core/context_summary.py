from __future__ import annotations

from core.context import ModelContext
from core.context.preparation import (
    SummaryGeneration,
    SummaryGenerationBlocked,
)
from core.model_call.service import (
    ModelCallBlocked,
    ModelCallRequest,
    ModelCallService,
)
from core.model_call.types import InvalidLLMResponse


class LLMContextSummaryGenerator:
    def __init__(
        self,
        model_calls: ModelCallService,
        model: str,
    ) -> None:
        self.model_calls = model_calls
        self.model = model

    def generate(
        self,
        model_context: ModelContext,
    ) -> SummaryGeneration | SummaryGenerationBlocked:
        outcome = self.model_calls.call(
            ModelCallRequest(context=model_context, tools=[]),
            self.model,
        )
        if isinstance(outcome, ModelCallBlocked):
            return SummaryGenerationBlocked(outcome.assessment)
        if outcome.response.type != "text":
            raise InvalidLLMResponse(
                "invalid_summary_response",
                "context summary response type must be text",
            )
        return SummaryGeneration(
            summary=outcome.response.content,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
        )
