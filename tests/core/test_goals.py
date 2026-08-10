import unittest

from core.goals import (
    DependencyChange,
    Goal,
    GoalStatus,
    OutcomeDecision,
    PlanRevision,
    TaskDraft,
    TaskOutcome,
    TaskStatus,
)


class GoalTest(unittest.TestCase):
    @staticmethod
    def make_goal() -> Goal:
        return Goal(
            goal_id="goal-1",
            objective="检查并修复两个问题",
            tasks=[
                TaskDraft("A", "定位两个问题"),
                TaskDraft("B", "修复问题一", depends_on=("A",)),
                TaskDraft("C", "修复问题二", depends_on=("A",)),
                TaskDraft("D", "整体验证", depends_on=("B", "C")),
            ],
        )

    @staticmethod
    def complete(goal: Goal, task_id: str, run_id: str) -> TaskOutcome:
        goal.start_task_run(task_id, run_id)
        goal.finish_task_run(run_id)
        outcome = TaskOutcome(
            task_id=task_id,
            run_id=run_id,
            decision=OutcomeDecision.COMPLETED,
            summary=f"{task_id} 已通过验收",
            evidence=("tests passed",),
        )
        goal.record_outcome(outcome)
        return outcome

    def test_next_task_uses_plan_order_among_satisfied_tasks(self):
        goal = self.make_goal()
        self.complete(goal, "A", "run-1")

        self.assertEqual(goal.next_task().id, "B")

    def test_continue_outcome_is_appended_and_keeps_task_active(self):
        goal = self.make_goal()
        self.complete(goal, "A", "run-1")
        goal.start_task_run("B", "run-2")
        goal.finish_task_run("run-2")
        outcome = TaskOutcome(
            task_id="B",
            run_id="run-2",
            decision=OutcomeDecision.CONTINUE,
            summary="修复尚未通过回归测试",
            evidence=("test_regression failed",),
        )

        goal.record_outcome(outcome)

        self.assertEqual(goal.outcomes[-1], outcome)
        self.assertEqual(goal.task("B").status, TaskStatus.ACTIVE)
        self.assertEqual(goal.next_task().id, "B")

    def test_same_run_cannot_submit_a_second_outcome(self):
        goal = self.make_goal()
        goal.start_task_run("A", "run-1")
        goal.finish_task_run("run-1")
        outcome = TaskOutcome(
            task_id="A",
            run_id="run-1",
            decision=OutcomeDecision.CONTINUE,
            summary="需要继续定位",
        )
        goal.record_outcome(outcome)

        with self.assertRaises(ValueError):
            goal.record_outcome(outcome)

        self.assertEqual(goal.outcomes, (outcome,))

    def test_run_must_bind_to_the_next_serial_task(self):
        goal = self.make_goal()
        self.complete(goal, "A", "run-1")

        with self.assertRaises(ValueError):
            goal.start_task_run("C", "run-2")

        self.assertEqual(goal.task("C").status, TaskStatus.PENDING)

    def test_outcome_must_match_the_bound_run_and_task(self):
        goal = self.make_goal()
        goal.start_task_run("A", "run-1")
        goal.finish_task_run("run-1")
        wrong = TaskOutcome(
            task_id="B",
            run_id="run-1",
            decision=OutcomeDecision.COMPLETED,
            summary="错误关联",
        )

        with self.assertRaises(ValueError):
            goal.record_outcome(wrong)

        self.assertEqual(goal.outcomes, ())
        self.assertEqual(goal.task("A").status, TaskStatus.ACTIVE)

    def test_replan_requires_an_explicit_revision(self):
        goal = self.make_goal()
        self.complete(goal, "A", "run-1")
        goal.start_task_run("B", "run-2")
        goal.finish_task_run("run-2")
        outcome = TaskOutcome(
            task_id="B",
            run_id="run-2",
            decision=OutcomeDecision.REPLAN,
            summary="修复引入新的回归",
            evidence=("test_y failed",),
        )

        goal.record_outcome(outcome)

        self.assertEqual(goal.status, GoalStatus.REPLAN_REQUIRED)
        self.assertEqual(goal.task("B").status, TaskStatus.ACTIVE)
        self.assertIsNone(goal.next_task())

    def test_revision_supersedes_task_and_inserts_replacements_in_place(self):
        goal = self.make_goal()
        self.complete(goal, "A", "run-1")
        goal.start_task_run("B", "run-2")
        goal.finish_task_run("run-2")
        goal.record_outcome(
            TaskOutcome(
                task_id="B",
                run_id="run-2",
                decision=OutcomeDecision.REPLAN,
                summary="需要先定位回归",
            )
        )
        revision = PlanRevision(
            task_id="B",
            reason="拆分定位和修复步骤",
            replacement_tasks=(
                TaskDraft("B1", "定位回归原因", depends_on=("A",)),
                TaskDraft("B2", "重新修复问题一", depends_on=("B1",)),
            ),
            dependency_changes=(
                DependencyChange("D", depends_on=("B2", "C")),
            ),
        )

        goal.apply_plan_revision(revision)

        self.assertEqual(goal.status, GoalStatus.ACTIVE)
        self.assertEqual(goal.task("B").status, TaskStatus.SUPERSEDED)
        self.assertEqual(
            [task.id for task in goal.tasks],
            ["A", "B", "B1", "B2", "C", "D"],
        )
        self.assertEqual(goal.next_task().id, "B1")

    def test_revision_is_atomic_when_a_task_still_depends_on_superseded_task(self):
        goal = Goal(
            goal_id="goal-1",
            objective="完成任务",
            tasks=[
                TaskDraft("A", "执行"),
                TaskDraft("B", "验证", depends_on=("A",)),
            ],
        )
        goal.start_task_run("A", "run-1")
        goal.finish_task_run("run-1")
        goal.record_outcome(
            TaskOutcome(
                task_id="A",
                run_id="run-1",
                decision=OutcomeDecision.REPLAN,
                summary="需要替换方案",
            )
        )
        revision = PlanRevision(
            task_id="A",
            reason="替换 A",
            replacement_tasks=(TaskDraft("A1", "新方案"),),
        )

        with self.assertRaises(ValueError):
            goal.apply_plan_revision(revision)

        self.assertEqual(goal.status, GoalStatus.REPLAN_REQUIRED)
        self.assertEqual([task.id for task in goal.tasks], ["A", "B"])
        self.assertEqual(goal.task("A").status, TaskStatus.ACTIVE)

    def test_revision_rejects_dependency_cycles_atomically(self):
        goal = Goal(
            goal_id="goal-1",
            objective="完成任务",
            tasks=[TaskDraft("A", "执行")],
        )
        goal.start_task_run("A", "run-1")
        goal.finish_task_run("run-1")
        goal.record_outcome(
            TaskOutcome(
                task_id="A",
                run_id="run-1",
                decision=OutcomeDecision.REPLAN,
                summary="需要拆分",
            )
        )
        revision = PlanRevision(
            task_id="A",
            reason="错误的循环方案",
            replacement_tasks=(
                TaskDraft("A1", "第一步", depends_on=("A2",)),
                TaskDraft("A2", "第二步", depends_on=("A1",)),
            ),
        )

        with self.assertRaises(ValueError):
            goal.apply_plan_revision(revision)

        self.assertEqual(goal.status, GoalStatus.REPLAN_REQUIRED)
        self.assertEqual([task.id for task in goal.tasks], ["A"])

    def test_goal_completes_after_all_effective_tasks_complete(self):
        goal = self.make_goal()

        self.complete(goal, "A", "run-1")
        self.complete(goal, "B", "run-2")
        self.complete(goal, "C", "run-3")
        self.complete(goal, "D", "run-4")

        self.assertEqual(goal.status, GoalStatus.COMPLETED)
        self.assertIsNone(goal.next_task())

    def test_finished_run_releases_execution_without_changing_task_result(self):
        goal = self.make_goal()
        goal.start_task_run("A", "run-1")

        goal.finish_task_run("run-1")

        self.assertEqual(goal.task("A").status, TaskStatus.ACTIVE)
        self.assertEqual(goal.outcomes, ())
        self.assertEqual(goal.run_links[0].goal_id, "goal-1")
        self.assertEqual(goal.run_links[0].task_id, "A")
        self.assertEqual(goal.run_links[0].run_id, "run-1")
        goal.start_task_run("A", "run-2")

    def test_outcome_requires_a_finished_run(self):
        goal = self.make_goal()
        goal.start_task_run("A", "run-1")
        outcome = TaskOutcome(
            task_id="A",
            run_id="run-1",
            decision=OutcomeDecision.COMPLETED,
            summary="尚未结束的 Run 不能提交结果",
        )

        with self.assertRaises(ValueError):
            goal.record_outcome(outcome)

        self.assertEqual(goal.outcomes, ())

    def test_initial_plan_rejects_unknown_dependency_and_cycle(self):
        with self.assertRaises(ValueError):
            Goal(
                "goal-1",
                "目标",
                [TaskDraft("A", "执行", depends_on=("missing",))],
            )

        with self.assertRaises(ValueError):
            Goal(
                "goal-1",
                "目标",
                [
                    TaskDraft("A", "第一步", depends_on=("B",)),
                    TaskDraft("B", "第二步", depends_on=("A",)),
                ],
            )


if __name__ == "__main__":
    unittest.main()
