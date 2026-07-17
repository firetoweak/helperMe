import json
import unittest
from unittest.mock import Mock

from core.context import (
    ContextBudget,
    ModelBudgetConfig,
    ModelContext,
    TiktokenTokenEstimator,
    make_budget_assessment,
)


class BudgetAssessmentTest(unittest.TestCase):
    def test_allowed_and_overflow_are_derived(self):
        allowed = make_budget_assessment(
            estimated_input_tokens=750,
            input_budget_tokens=750,
        )
        exceeded = make_budget_assessment(
            estimated_input_tokens=820,
            input_budget_tokens=750,
        )

        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.overflow_tokens, 0)
        self.assertFalse(exceeded.allowed)
        self.assertEqual(exceeded.overflow_tokens, 70)


class ModelBudgetConfigTest(unittest.TestCase):
    def test_rejects_invalid_external_configuration(self):
        for context_limit, input_ratio in (
            (0, 0.75),
            (-1, 0.75),
            (1000, 0),
            (1000, 1),
        ):
            with self.subTest(
                context_limit=context_limit,
                input_ratio=input_ratio,
            ):
                with self.assertRaises(ValueError):
                    ModelBudgetConfig(context_limit, input_ratio)


class ContextBudgetTest(unittest.TestCase):
    def test_assess_uses_project_ratio_without_mutating_context(self):
        estimator = Mock()
        composition = make_budget_assessment(751, 750).composition
        estimator.breakdown.return_value = composition
        context = ModelContext(
            messages=[{"role": "user", "content": "hello"}]
        )
        original_messages = [message.copy() for message in context.messages]
        tools = [{"type": "function"}]
        budget = ContextBudget(
            estimator=estimator,
            config=ModelBudgetConfig(
                context_limit=1000,
                input_ratio=0.75,
            ),
        )

        assessment = budget.assess(context, tools)

        self.assertEqual(assessment.input_budget_tokens, 750)
        self.assertEqual(assessment.estimated_input_tokens, 751)
        self.assertFalse(assessment.allowed)
        self.assertEqual(context.messages, original_messages)
        self.assertIs(assessment.composition, composition)
        estimator.breakdown.assert_called_once_with(
            context,
            tools,
            input_budget_tokens=750,
        )

    def test_observe_actual_usage_only_delegates_calibration(self):
        estimator = Mock()
        context = ModelContext(messages=[])
        tools = []
        budget = ContextBudget(
            estimator=estimator,
            config=ModelBudgetConfig(1000, 0.75),
        )

        budget.observe_actual_usage(context, tools, 420)

        estimator.calibrate.assert_called_once_with(context, tools, 420)


class TiktokenTokenEstimatorTest(unittest.TestCase):
    def test_estimates_messages_and_tools_with_one_template(self):
        estimator = TiktokenTokenEstimator()
        context = ModelContext(
            messages=[{"role": "system", "content": "系统"}]
        )
        tools = [{"type": "function", "name": "read_file"}]

        with_tools = estimator.estimate(context, tools)
        without_tools = estimator.estimate(context, [])

        self.assertGreater(with_tools, without_tools)

    def test_calibration_uses_largest_coefficient_in_recent_window(self):
        estimator = TiktokenTokenEstimator(window_size=2)
        context = ModelContext(
            messages=[{"role": "user", "content": "hello"}]
        )
        tools = []
        initial_estimate = estimator.estimate(context, tools)

        estimator.calibrate(context, tools, initial_estimate * 3)
        estimator.calibrate(context, tools, initial_estimate * 2)

        self.assertEqual(estimator.coefficient, 3.0)

        estimator.calibrate(context, tools, initial_estimate)

        self.assertEqual(estimator.coefficient, 2.0)

    def test_calibration_coefficient_does_not_fall_below_one(self):
        estimator = TiktokenTokenEstimator()
        context = ModelContext(
            messages=[{"role": "user", "content": "hello"}]
        )
        tools = []

        estimator.calibrate(context, tools, actual_input_tokens=1)

        self.assertEqual(estimator.coefficient, 1.0)

    def test_rejects_invalid_window_size(self):
        with self.assertRaises(ValueError):
            TiktokenTokenEstimator(window_size=0)

    def test_breakdown_roles_and_tools_sum_to_estimated_total(self):
        estimator = TiktokenTokenEstimator()
        context = ModelContext(
            messages=[
                {"role": "system", "content": "系统指令"},
                {"role": "user", "content": "请搜索"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "grep",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "x" * 5_000,
                },
            ]
        )
        tools = [{"type": "function", "function": {"name": "grep"}}]

        composition = estimator.breakdown(
            context,
            tools,
            input_budget_tokens=10_000,
        )
        role_sum = sum(composition.by_role_tokens.values())
        total_parts = role_sum + composition.tools_schema_tokens

        self.assertEqual(
            composition.estimated_total_tokens,
            estimator.estimate(context, tools),
        )
        self.assertEqual(total_parts, composition.estimated_total_tokens)
        self.assertGreater(composition.by_role_tokens["tool"], 0)
        self.assertGreater(composition.tools_schema_tokens, 0)
        self.assertEqual(composition.tool_result_chars, 5_000)
        self.assertEqual(composition.input_budget_tokens, 10_000)
        self.assertEqual(len(composition.tool_results), 1)
        tool_stat = composition.tool_results[0]
        self.assertEqual(tool_stat.tool_call_id, "call_1")
        self.assertEqual(tool_stat.tool_name, "grep")
        self.assertEqual(tool_stat.chars, 5_000)
        self.assertFalse(tool_stat.externalized)
        self.assertIsNone(tool_stat.artifact_id)
        self.assertEqual(
            sum(item.estimated_tokens for item in composition.tool_results),
            composition.by_role_tokens["tool"],
        )
        self.assertLess(
            composition.dehydrated_tool_tokens_estimate,
            composition.by_role_tokens["tool"],
        )
        self.assertGreater(
            composition.to_dict()["dehydrated_tool_savings_estimate"],
            0,
        )

    def test_breakdown_marks_externalized_tool_results(self):
        estimator = TiktokenTokenEstimator()
        content = json.dumps(
            {
                "ok": True,
                "code": "OK",
                "data": {
                    "externalized": True,
                    "artifact_id": "art_abc",
                    "size_chars": 20_000,
                    "preview": "head",
                },
                "error": None,
                "hint": "read_artifact",
            },
            ensure_ascii=False,
        )
        context = ModelContext(
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_ext",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_ext",
                    "content": content,
                },
            ]
        )

        composition = estimator.breakdown(
            context,
            [],
            input_budget_tokens=10_000,
        )

        self.assertEqual(len(composition.tool_results), 1)
        self.assertTrue(composition.tool_results[0].externalized)
        self.assertEqual(
            composition.tool_results[0].artifact_id,
            "art_abc",
        )
        self.assertEqual(
            composition.tool_results[0].tool_name,
            "read_file",
        )


if __name__ == "__main__":
    unittest.main()
