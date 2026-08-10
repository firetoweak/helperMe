import json
import unittest
from unittest.mock import Mock

from core.goals import (
    GoalApplicationService,
    GoalCommandBuffer,
    GoalCommandBufferRegistry,
    GoalCommandKind,
    GoalStatus,
    InMemoryGoalStore,
    OutcomeDecision,
    PlanRevision,
    TaskDraft,
    TaskOutcome,
    TaskStatus,
    CommandRequirement,
    TaskVerification,
)
from core.session import SessionRuntime
from core.tools_runtime.run_runtime import RunStatus
from core.tools_runtime import RunEvidence, ToolEvidence


class InMemoryGoalStoreTest(unittest.TestCase):
    @staticmethod
    def make_goal(service, session_id="session-1", goal_id="goal-1"):
        return service.create_goal(
            session_id,
            goal_id,
            "完成任务",
            [TaskDraft("A", "执行任务")],
        )

    def setUp(self):
        self.sessions = SessionRuntime(run_runtime=Mock())
        self.sessions.create_session("session-1", "system prompt")
        self.store = InMemoryGoalStore()
        self.service = GoalApplicationService(
            self.sessions,
            self.store,
            GoalCommandBufferRegistry(),
        )

    def test_session_can_have_only_one_uncompleted_goal(self):
        first = self.make_goal(self.service)

        with self.assertRaises(ValueError):
            self.make_goal(self.service, goal_id="goal-2")

        self.assertIs(self.store.active_for_session("session-1"), first)

    def test_completed_goal_allows_a_new_goal_in_the_same_session(self):
        first = self.make_goal(self.service)
        first.start_task_run("A", "run-1")
        first.finish_task_run("run-1")
        first.record_outcome(
            TaskOutcome(
                task_id="A",
                run_id="run-1",
                decision=OutcomeDecision.COMPLETED,
                summary="完成",
            )
        )

        second = self.make_goal(self.service, goal_id="goal-2")

        self.assertIs(self.store.active_for_session("session-1"), second)

    def test_goal_requires_an_existing_session(self):
        with self.assertRaises(KeyError):
            self.make_goal(self.service, session_id="missing")

        self.assertIsNone(self.store.active_for_session("missing"))


class GoalCommandBufferTest(unittest.TestCase):
    def test_buffer_keeps_only_the_latest_outcome_draft_for_its_run(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-1",
            expected_kind=GoalCommandKind.TASK_OUTCOME,
        )
        outcome = TaskOutcome(
            task_id="A",
            run_id="run-1",
            decision=OutcomeDecision.CONTINUE,
            summary="继续",
        )

        buffer.submit_task_outcome(outcome)
        revised = TaskOutcome(
            task_id="A",
            run_id="run-1",
            decision=OutcomeDecision.REPLAN,
            summary="修订结论",
        )
        buffer.submit_task_outcome(revised)

        self.assertIs(buffer.task_outcome, revised)

    def test_buffer_rejects_a_command_for_another_task_or_run(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-1",
            expected_kind=GoalCommandKind.TASK_OUTCOME,
        )

        for task_id, run_id in (("B", "run-1"), ("A", "run-2")):
            with self.subTest(task_id=task_id, run_id=run_id):
                with self.assertRaises(ValueError):
                    buffer.submit_task_outcome(
                        TaskOutcome(
                            task_id=task_id,
                            run_id=run_id,
                            decision=OutcomeDecision.CONTINUE,
                            summary="错误关联",
                        )
                    )

    def test_outcome_run_does_not_accept_a_plan_revision(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-1",
            expected_kind=GoalCommandKind.TASK_OUTCOME,
        )

        with self.assertRaises(ValueError):
            buffer.submit_plan_revision(
                PlanRevision(
                    task_id="A",
                    reason="调整",
                    replacement_tasks=(TaskDraft("A1", "新任务"),),
                )
            )

    def test_plan_revision_buffer_accepts_only_its_bound_task(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-2",
            expected_kind=GoalCommandKind.PLAN_REVISION,
        )
        revision = PlanRevision(
            task_id="A",
            reason="拆分任务",
            replacement_tasks=(TaskDraft("A1", "新任务"),),
        )

        buffer.submit_plan_revision(revision)

        self.assertIs(buffer.plan_revision, revision)
        with self.assertRaises(ValueError):
            buffer.submit_plan_revision(revision)


class GoalApplicationServiceTest(unittest.TestCase):
    def setUp(self):
        self.run_runtime = Mock()
        self.sessions = SessionRuntime(run_runtime=self.run_runtime)
        self.session = self.sessions.create_session(
            "session-1",
            "system prompt",
        )
        self.store = InMemoryGoalStore()
        self.buffers = GoalCommandBufferRegistry()
        self.service = GoalApplicationService(
            self.sessions,
            self.store,
            self.buffers,
        )
        self.goal = self.service.create_goal(
            "session-1",
            "goal-1",
            "完成任务",
            [TaskDraft("A", "执行任务")],
        )

    def result(self, status: RunStatus):
        return Mock(
            status=status,
            final_reason=(None if status is RunStatus.COMPLETED else "stopped"),
            context_state=self.session.context_state,
            evidence=RunEvidence(),
        )

    def submit_during_run(
        self,
        run_id: str,
        decision: OutcomeDecision,
    ):
        def run(**_kwargs):
            self.buffers.get("goal-1", run_id).submit_task_outcome(
                TaskOutcome(
                    task_id="A",
                    run_id=run_id,
                    decision=decision,
                    summary="模型提交的显式结果",
                )
            )
            return self.result(RunStatus.COMPLETED)

        return run

    def test_completed_run_commits_buffered_outcome_after_runtime_returns(self):
        self.run_runtime.run.side_effect = self.submit_during_run(
            "run-1",
            OutcomeDecision.COMPLETED,
        )

        result = self.service.execute_next_task(
            "session-1",
            "goal-1",
            "run-1",
            "执行任务",
        )

        self.assertIs(result.applied_outcome, self.goal.outcomes[0])
        self.assertEqual(self.goal.status, GoalStatus.COMPLETED)
        with self.assertRaises(KeyError):
            self.buffers.get("goal-1", "run-1")

    def test_failed_real_evidence_rejects_model_completion_until_later_run_passes(self):
        sessions = SessionRuntime(run_runtime=Mock())
        session = sessions.create_session("verified-session", "system prompt")
        buffers = GoalCommandBufferRegistry()
        service = GoalApplicationService(
            sessions,
            InMemoryGoalStore(),
            buffers,
        )
        goal = service.create_goal(
            session.id,
            "verified-goal",
            "完成测试",
            [
                TaskDraft(
                    "A",
                    "运行测试",
                    acceptance_criteria="测试通过",
                    verification=TaskVerification(
                        commands=(
                            CommandRequirement("python -m unittest"),
                        )
                    ),
                )
            ],
        )

        def execute(run_id: str, exit_code: int):
            def run(**_kwargs):
                buffers.get("verified-goal", run_id).submit_task_outcome(
                    TaskOutcome(
                        task_id="A",
                        run_id=run_id,
                        decision=OutcomeDecision.COMPLETED,
                        summary="模型声称测试通过",
                        evidence=("tests passed",),
                    )
                )
                return Mock(
                    status=RunStatus.COMPLETED,
                    final_reason=None,
                    context_state=session.context_state,
                    evidence=RunEvidence(
                        (
                            ToolEvidence(
                                "command-1",
                                "execute_command",
                                json.dumps(
                                    {
                                        "root": "workspace",
                                        "cwd": ".",
                                        "command": "python -m unittest -v",
                                    }
                                ),
                                {
                                    "ok": True,
                                    "code": "COMMAND_COMPLETED",
                                    "data": {
                                        "exit_code": exit_code,
                                        "timed_out": False,
                                    },
                                },
                            ),
                        )
                    ),
                )

            return run

        sessions.run_runtime.run.side_effect = execute("run-1", 1)
        rejected = service.execute_next_task(
            session.id,
            goal.id,
            "run-1",
            "执行测试",
        )

        self.assertIsNone(rejected.applied_outcome)
        self.assertFalse(rejected.completion_review.accepted)
        self.assertEqual(goal.task("A").status, TaskStatus.ACTIVE)

        sessions.run_runtime.run.side_effect = execute("run-2", 0)
        accepted = service.execute_next_task(
            session.id,
            goal.id,
            "run-2",
            "修复后重新测试",
        )

        self.assertTrue(accepted.completion_review.accepted)
        self.assertEqual(goal.status, GoalStatus.COMPLETED)

    def test_service_passes_a_bound_goal_capability_to_session_runtime(self):
        captured = {}

        def run(**kwargs):
            captured["invocation"] = kwargs["invocation"]
            self.buffers.get("goal-1", "run-1").submit_task_outcome(
                TaskOutcome(
                    task_id="A",
                    run_id="run-1",
                    decision=OutcomeDecision.CONTINUE,
                    summary="继续",
                )
            )
            return self.result(RunStatus.COMPLETED)

        self.run_runtime.run.side_effect = run
        self.service.execute_next_task(
            "session-1",
            "goal-1",
            "run-1",
            "执行任务",
        )

        capability = captured["invocation"].capabilities[0]
        self.assertEqual(capability.goal_id, "goal-1")
        self.assertEqual(capability.task.id, "A")

    def test_completed_run_without_buffered_command_does_not_mutate_goal(self):
        self.run_runtime.run.return_value = self.result(RunStatus.COMPLETED)

        result = self.service.execute_next_task(
            "session-1",
            "goal-1",
            "run-1",
            "执行任务",
        )
        self.assertIsNone(result.applied_outcome)
        self.assertEqual(self.goal.outcomes, ())
        self.assertEqual(self.goal.status, GoalStatus.ACTIVE)

    def test_noncompleted_run_releases_run_but_keeps_task_active(self):
        for status in (
            RunStatus.INTERRUPTED,
            RunStatus.BLOCKED,
            RunStatus.FAILED,
        ):
            with self.subTest(status=status):
                self.setUp()
                self.run_runtime.run.return_value = self.result(status)
                self.service.execute_next_task(
                    "session-1",
                    "goal-1",
                    "run-1",
                    "执行任务",
                )

                self.assertEqual(self.goal.task("A").status, TaskStatus.ACTIVE)
                self.assertEqual(self.goal.outcomes, ())

                self.run_runtime.run.side_effect = self.submit_during_run(
                    "run-2",
                    OutcomeDecision.COMPLETED,
                )
                self.service.execute_next_task(
                    "session-1",
                    "goal-1",
                    "run-2",
                    "继续执行",
                )
                self.assertEqual(self.goal.status, GoalStatus.COMPLETED)

    def test_runtime_exception_aborts_goal_run_and_releases_command_buffer(self):
        self.run_runtime.run.side_effect = RuntimeError("runtime bug")

        with self.assertRaisesRegex(RuntimeError, "runtime bug"):
            self.service.execute_next_task(
                "session-1",
                "goal-1",
                "run-1",
                "执行任务",
            )

        self.assertEqual(self.goal.task("A").status, TaskStatus.ACTIVE)
        with self.assertRaises(KeyError):
            self.buffers.get("goal-1", "run-1")
        self.assertEqual(self.session.status.value, "failed")

        self.run_runtime.run.side_effect = self.submit_during_run(
            "run-2",
            OutcomeDecision.COMPLETED,
        )
        self.service.execute_next_task(
            "session-1",
            "goal-1",
            "run-2",
            "继续执行",
        )
        self.assertEqual(self.goal.status, GoalStatus.COMPLETED)

    def test_goal_cannot_run_through_another_session(self):
        self.sessions.create_session("session-2", "system prompt")

        with self.assertRaises(ValueError):
            self.service.execute_next_task(
                "session-2",
                "goal-1",
                "run-1",
                "执行任务",
            )

        self.assertEqual(self.goal.task("A").status, TaskStatus.PENDING)

    def test_invalid_run_input_is_rejected_before_goal_mutation(self):
        with self.assertRaises(ValueError):
            self.service.execute_next_task(
                "session-1",
                "goal-1",
                "run-1",
                "   ",
            )

        self.assertEqual(self.goal.task("A").status, TaskStatus.PENDING)
        self.assertEqual(self.goal.run_links, ())

    def test_plan_revision_is_committed_only_after_its_run_completes(self):
        self.run_runtime.run.side_effect = self.submit_during_run(
            "run-1",
            OutcomeDecision.REPLAN,
        )
        self.service.execute_next_task(
            "session-1",
            "goal-1",
            "run-1",
            "执行任务",
        )

        def submit_revision(**_kwargs):
            self.buffers.get("goal-1", "run-2").submit_plan_revision(
                PlanRevision(
                    task_id="A",
                    reason="替换原任务",
                    replacement_tasks=(TaskDraft("A1", "新执行方案"),),
                )
            )
            self.assertEqual(self.goal.status, GoalStatus.REPLAN_REQUIRED)
            return self.result(RunStatus.COMPLETED)

        self.run_runtime.run.side_effect = submit_revision
        result = self.service.execute_plan_revision(
            "session-1",
            "goal-1",
            "run-2",
            "重新规划",
        )

        self.assertIsNotNone(result.applied_revision)
        self.assertEqual(self.goal.status, GoalStatus.ACTIVE)
        self.assertEqual(self.goal.task("A").status, TaskStatus.SUPERSEDED)
        self.assertEqual(self.goal.next_task().id, "A1")

    def test_noncompleted_plan_run_keeps_goal_waiting_for_revision(self):
        self.run_runtime.run.side_effect = self.submit_during_run(
            "run-1",
            OutcomeDecision.REPLAN,
        )
        self.service.execute_next_task(
            "session-1",
            "goal-1",
            "run-1",
            "执行任务",
        )
        self.run_runtime.run.side_effect = None
        self.run_runtime.run.return_value = self.result(RunStatus.BLOCKED)

        result = self.service.execute_plan_revision(
            "session-1",
            "goal-1",
            "run-2",
            "重新规划",
        )

        self.assertIsNone(result.applied_revision)
        self.assertEqual(self.goal.status, GoalStatus.REPLAN_REQUIRED)


if __name__ == "__main__":
    unittest.main()
