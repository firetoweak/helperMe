import unittest

from core.planner import build_runtime_messages, create_plan, format_plan_for_model


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


if __name__ == "__main__":
    unittest.main()
