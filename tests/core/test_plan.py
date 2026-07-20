import unittest

from core.planning import Plan, PlanStep


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
                "revision": 1,
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

    def test_revise_replaces_unfinished_suffix_and_preserves_history(self):
        plan = Plan(
            goal="完成任务",
            revision=1,
            steps=[
                PlanStep(id=1, text="分析", status="done", note="已完成分析"),
                PlanStep(id=2, text="调用接口", status="doing"),
                PlanStep(id=3, text="处理结果", status="pending"),
            ],
        )

        plan.revise(
            reason="接口不存在",
            remaining_steps=["检查本地数据", "验证结果"],
        )

        self.assertEqual(plan.goal, "完成任务")
        self.assertEqual(plan.revision, 2)
        self.assertEqual(
            [
                (step.id, step.text, step.status, step.note)
                for step in plan.steps
            ],
            [
                (1, "分析", "done", "已完成分析"),
                (2, "调用接口", "skipped", "接口不存在"),
                (4, "检查本地数据", "doing", None),
                (5, "验证结果", "pending", None),
            ],
        )

    def test_revise_keeps_step_ids_monotonic_across_revisions(self):
        plan = Plan(
            goal="完成任务",
            steps=[
                PlanStep(id=1, text="分析", status="done"),
                PlanStep(id=2, text="首次方案", status="doing"),
                PlanStep(id=3, text="首次验证", status="pending"),
            ],
        )

        plan.revise("首次方案失败", ["第二套方案", "第二次验证"])
        plan.mark_done(4)
        plan.revise("验证路径失效", ["最终验证"])

        self.assertEqual(plan.revision, 3)
        self.assertEqual(
            [(step.id, step.text, step.status) for step in plan.steps],
            [
                (1, "分析", "done"),
                (2, "首次方案", "skipped"),
                (4, "第二套方案", "done"),
                (6, "最终验证", "doing"),
            ],
        )

    def test_complete_remaining_marks_all_active_steps_done(self):
        plan = Plan(
            goal="完成任务",
            steps=[
                PlanStep(id=1, text="已完成", status="done", note="原说明"),
                PlanStep(id=2, text="当前步骤", status="doing"),
                PlanStep(id=3, text="后续步骤", status="pending"),
                PlanStep(id=4, text="无需执行", status="skipped", note="已跳过"),
            ],
        )

        plan.complete_remaining("已完成最终回答")

        self.assertEqual(
            [(step.status, step.note) for step in plan.steps],
            [
                ("done", "原说明"),
                ("done", "已完成最终回答"),
                ("done", "已完成最终回答"),
                ("skipped", "已跳过"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
