import unittest

from core.plan import Plan, PlanStep


class PlanTest(unittest.TestCase):
    def test_status_changes_and_current_step(self):
        plan = Plan(
            goal="测试目标",
            steps=[
                PlanStep(id=1, text="理解任务"),
                PlanStep(id=2, text="执行任务"),
            ],
        )

        self.assertEqual(plan.current_step().id, 1)

        plan.mark_doing(1, "开始")
        self.assertEqual(plan.current_step().id, 1)
        self.assertEqual(plan.current_step().status, "doing")

        plan.mark_done(1, "完成理解")
        self.assertEqual(plan.current_step().id, 2)
        self.assertEqual(plan.current_step().status, "pending")

    def test_to_dict_is_json_friendly(self):
        plan = Plan(
            goal="测试目标",
            steps=[PlanStep(id=1, text="理解任务", status="doing", note="开始")],
        )

        self.assertEqual(
            plan.to_dict(),
            {
                "goal": "测试目标",
                "steps": [
                    {
                        "id": 1,
                        "text": "理解任务",
                        "status": "doing",
                        "note": "开始",
                    }
                ],
            },
        )

    def test_get_step_raises_for_unknown_id(self):
        plan = Plan(goal="测试目标", steps=[])

        with self.assertRaises(ValueError):
            plan.get_step(1)


if __name__ == "__main__":
    unittest.main()
