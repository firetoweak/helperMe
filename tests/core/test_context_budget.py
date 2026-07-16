import unittest
from unittest.mock import Mock

from core.context import (
    BudgetAssessment,
    ContextBudget,
    ModelBudgetConfig,
    ModelContext,
    TiktokenTokenEstimator,
)


class BudgetAssessmentTest(unittest.TestCase):
    def test_allowed_and_overflow_are_derived(self):
        allowed = BudgetAssessment(
            estimated_input_tokens=750,
            input_budget_tokens=750,
        )
        exceeded = BudgetAssessment(
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
        estimator.estimate.return_value = 751
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
        estimator.estimate.assert_called_once_with(context, tools)

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


if __name__ == "__main__":
    unittest.main()
