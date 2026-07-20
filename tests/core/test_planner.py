import unittest

from core.model_call import InvalidLLMResponse, LLMResponse, ToolCall
from core.planning import (
    create_plan,
    format_plan_for_model,
    InvalidPlanResponse,
    parse_plan_response,
)


class PlannerTest(unittest.TestCase):
    def test_create_and_format_plan_from_model_response(self):
        plan = create_plan(
            LLMResponse(
                type="text",
                content=(
                    '{"goal":"帮我分析项目",'
                    '"steps":["读取项目","分析结构"]}'
                ),
            )
        )

        text = format_plan_for_model(plan)

        self.assertEqual(plan.goal, "帮我分析项目")
        self.assertEqual(len(plan.steps), 2)
        self.assertIn("当前执行计划", text)
        self.assertIn("[pending]", text)

    def test_create_plan_invalid_json_exposes_raw_response(self):
        with self.assertRaises(InvalidLLMResponse) as raised:
            create_plan(
                LLMResponse(type="text", content="先分析一下再制定计划")
            )

        self.assertEqual(raised.exception.code, "invalid_plan_response")
        self.assertIn(
            "raw_response='先分析一下再制定计划'",
            str(raised.exception),
        )

    def test_create_plan_rejects_non_text_response(self):
        with self.assertRaises(InvalidLLMResponse) as raised:
            create_plan(
                LLMResponse(
                    type="tool_calls",
                    calls=[
                        ToolCall(
                            id="call-unexpected",
                            name="unexpected_tool",
                            arguments="{}",
                        )
                    ],
                )
            )

        self.assertEqual(raised.exception.code, "invalid_plan_response")

    def test_parse_valid_plan_response(self):
        plan = parse_plan_response(
            '{"goal": "分析项目结构", "steps": ["读取文件", "分析职责", "给出建议"]}'
        )

        self.assertEqual(plan.goal, "分析项目结构")
        self.assertEqual(
            [step.text for step in plan.steps],
            ["读取文件", "分析职责", "给出建议"],
        )
        self.assertEqual([step.id for step in plan.steps], [1, 2, 3])
        self.assertTrue(all(step.status == "pending" for step in plan.steps))

    def test_parse_invalid_json_fails(self):
        with self.assertRaises(InvalidPlanResponse):
            parse_plan_response("不是 JSON")

    def test_parse_too_few_steps_fails(self):
        with self.assertRaises(InvalidPlanResponse):
            parse_plan_response('{"goal": "目标", "steps": ["只有一步"]}')

    def test_parse_too_many_steps_fails(self):
        with self.assertRaises(InvalidPlanResponse):
            parse_plan_response(
                '{"goal": "目标", "steps": ["1", "2", "3", "4", "5", "6", "7"]}'
            )

    def test_parse_invalid_goal_or_step_fails(self):
        invalid_responses = (
            '{"goal": "", "steps": ["1", "2"]}',
            '{"goal": "目标", "steps": ["1", null]}',
            '{"goal": "目标", "steps": ["1", ""]}',
        )

        for content in invalid_responses:
            with self.subTest(content=content):
                with self.assertRaises(InvalidPlanResponse):
                    parse_plan_response(content)


if __name__ == "__main__":
    unittest.main()
