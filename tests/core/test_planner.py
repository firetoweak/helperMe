import unittest

from core.planning import (
    build_plan_messages,
    build_runtime_messages,
    create_plan,
    format_plan_for_model,
    parse_plan_response,
)


class PlannerTest(unittest.TestCase):
    def test_build_runtime_messages_adds_plan_to_existing_system(self):
        messages = [
            {"role": "system", "content": "基础规则"},
            {"role": "user", "content": "你好"},
        ]

        runtime_messages = build_runtime_messages(messages, "计划内容")

        self.assertIn("基础规则", runtime_messages[0]["content"])
        self.assertIn("当前运行计划", runtime_messages[0]["content"])
        self.assertIn("计划内容", runtime_messages[0]["content"])
        self.assertEqual(messages[0]["content"], "基础规则")

    def test_build_runtime_messages_inserts_system_when_missing(self):
        messages = [{"role": "user", "content": "你好"}]

        runtime_messages = build_runtime_messages(messages, "计划内容")

        self.assertEqual(runtime_messages[0]["role"], "system")
        self.assertIn("当前运行计划", runtime_messages[0]["content"])
        self.assertEqual(messages[0]["role"], "user")

    def test_create_and_format_plan(self):
        plan = create_plan("帮我分析项目", None)
        text = format_plan_for_model(plan)

        self.assertEqual(plan.goal, "帮我分析项目")
        self.assertEqual(len(plan.steps), 4)
        self.assertIn("当前执行计划", text)
        self.assertIn("[pending]", text)

    def test_build_plan_messages_requires_json_only(self):
        messages = build_plan_messages("帮我分析项目", None)

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("只返回 JSON", messages[0]["content"])
        self.assertEqual(messages[1], {"role": "user", "content": "帮我分析项目"})

    def test_parse_valid_plan_response(self):
        plan = parse_plan_response(
            "原始请求",
            '{"goal": "分析项目结构", "steps": ["读取文件", "分析职责", "给出建议"]}',
        )

        self.assertEqual(plan.goal, "分析项目结构")
        self.assertEqual(
            [step.text for step in plan.steps],
            ["读取文件", "分析职责", "给出建议"],
        )
        self.assertEqual([step.id for step in plan.steps], [1, 2, 3])
        self.assertTrue(all(step.status == "pending" for step in plan.steps))

    def test_parse_invalid_json_falls_back(self):
        plan = parse_plan_response("原始请求", "不是 JSON")

        self.assertEqual(plan.goal, "原始请求")
        self.assertEqual(len(plan.steps), 4)

    def test_parse_too_few_steps_falls_back(self):
        plan = parse_plan_response(
            "原始请求",
            '{"goal": "目标", "steps": ["只有一步"]}',
        )

        self.assertEqual(plan.goal, "原始请求")
        self.assertEqual(len(plan.steps), 4)

    def test_parse_too_many_steps_is_truncated(self):
        plan = parse_plan_response(
            "原始请求",
            '{"goal": "目标", "steps": ["1", "2", "3", "4", "5", "6", "7"]}',
        )

        self.assertEqual(plan.goal, "目标")
        self.assertEqual(len(plan.steps), 6)
        self.assertEqual(plan.steps[-1].text, "6")


if __name__ == "__main__":
    unittest.main()
