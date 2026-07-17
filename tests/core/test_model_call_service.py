import unittest
from unittest.mock import Mock

from core.context import ModelContext, make_budget_assessment
from core.model_call.service import (
    ModelCallBlocked,
    ModelCallRequest,
    ModelCallService,
)
from core.model_call.types import LLMCallResult, LLMResponse, LLMUsage


class ModelCallServiceTest(unittest.TestCase):
    def test_budget_exceeded_does_not_call_model(self):
        llm_client = Mock()
        context_budget = Mock()
        assessment = make_budget_assessment(
            estimated_input_tokens=801,
            input_budget_tokens=750,
        )
        context_budget.assess.return_value = assessment
        request = ModelCallRequest(
            context=ModelContext(messages=[]),
            tools=[],
        )

        outcome = ModelCallService(
            llm_client=llm_client,
            context_budget=context_budget,
        ).call(request, "test-model")

        self.assertIsInstance(outcome, ModelCallBlocked)
        self.assertIs(outcome.assessment, assessment)
        llm_client.chat.assert_not_called()
        context_budget.observe_actual_usage.assert_not_called()

    def test_success_calibrates_with_real_input_usage(self):
        llm_client = Mock()
        context_budget = Mock()
        context_budget.assess.return_value = make_budget_assessment(
            estimated_input_tokens=700,
            input_budget_tokens=750,
        )
        call_result = LLMCallResult(
            response=LLMResponse(type="text", content="done"),
            usage=LLMUsage(input_tokens=680, output_tokens=20),
        )
        llm_client.chat.return_value = call_result
        context = ModelContext(
            messages=[{"role": "user", "content": "hello"}]
        )
        tools = [{"type": "function"}]
        request = ModelCallRequest(context=context, tools=tools)

        outcome = ModelCallService(
            llm_client=llm_client,
            context_budget=context_budget,
        ).call(request, "test-model")

        self.assertIs(outcome, call_result)
        llm_client.chat.assert_called_once_with(
            context.messages,
            "test-model",
            tools,
        )
        context_budget.observe_actual_usage.assert_called_once_with(
            context,
            tools,
            680,
        )


if __name__ == "__main__":
    unittest.main()
