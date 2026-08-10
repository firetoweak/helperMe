import json
import unittest
from unittest.mock import Mock

from core.goals import (
    CommandRequirement,
    DependencyChange,
    GoalApplicationService,
    GoalCommandBufferRegistry,
    GoalStatus,
    InMemoryGoalStore,
    OutcomeDecision,
    PlanRevision,
    TaskDraft,
    TaskOutcome,
    TaskStatus,
    TaskVerification,
)
from core.session import SessionRuntime
from core.tools_runtime import RunEvidence, ToolEvidence
from core.tools_runtime.run_runtime import RunStatus


def successful_command(call_id: str, command: str) -> ToolEvidence:
    return ToolEvidence(
        call_id,
        "execute_command",
        json.dumps(
            {
                "root": "project",
                "cwd": ".",
                "command": command,
            }
        ),
        {
            "ok": True,
            "code": "COMMAND_COMPLETED",
            "data": {"exit_code": 0, "timed_out": False},
        },
    )


class GoalFullLoopTest(unittest.TestCase):
    def test_replan_replacement_and_final_verification_complete_goal(self):
        runtime = Mock()
        sessions = SessionRuntime(run_runtime=runtime)
        session = sessions.create_session("session-1", "system prompt")
        buffers = GoalCommandBufferRegistry()
        service = GoalApplicationService(
            sessions,
            InMemoryGoalStore(),
            buffers,
        )
        goal = service.create_goal(
            session.id,
            "goal-1",
            "修复并验证项目",
            [
                TaskDraft(
                    "C",
                    "修改不存在的构建脚本",
                    acceptance_criteria="只能修改已存在的脚本，否则重规划",
                ),
                TaskDraft(
                    "D",
                    "整体验证",
                    depends_on=("C",),
                    acceptance_criteria="测试和构建均通过",
                    verification=TaskVerification(
                        commands=(
                            CommandRequirement("python -m unittest"),
                            CommandRequirement("scripts/build.py"),
                        )
                    ),
                ),
            ],
        )

        def result(evidence: RunEvidence = RunEvidence()):
            return Mock(
                status=RunStatus.COMPLETED,
                final_reason=None,
                context_state=session.context_state,
                evidence=evidence,
            )

        def request_replan(**_kwargs):
            buffers.get(goal.id, "run-1").submit_task_outcome(
                TaskOutcome(
                    "C",
                    "run-1",
                    OutcomeDecision.REPLAN,
                    "目标文件不存在，原约束不可满足",
                )
            )
            return result()

        runtime.run.side_effect = request_replan
        service.execute_next_task(
            session.id,
            goal.id,
            "run-1",
            "执行当前任务",
        )
        self.assertEqual(goal.status, GoalStatus.REPLAN_REQUIRED)

        replacement_verification = TaskVerification(
            commands=(CommandRequirement("scripts/build.py"),)
        )

        def revise_plan(**_kwargs):
            buffers.get(goal.id, "run-2").submit_plan_revision(
                PlanRevision(
                    task_id="C",
                    reason="允许创建缺失的构建脚本",
                    replacement_tasks=(
                        TaskDraft(
                            "C1",
                            "创建构建脚本并验证",
                            acceptance_criteria="构建命令通过",
                            verification=replacement_verification,
                        ),
                    ),
                    dependency_changes=(DependencyChange("D", ("C1",)),),
                )
            )
            return result()

        runtime.run.side_effect = revise_plan
        service.execute_plan_revision(
            session.id,
            goal.id,
            "run-2",
            "重规划",
        )
        self.assertEqual(goal.task("C").status, TaskStatus.SUPERSEDED)
        self.assertIs(goal.task("C1").verification, replacement_verification)

        def complete_replacement(**_kwargs):
            buffers.get(goal.id, "run-3").submit_task_outcome(
                TaskOutcome(
                    "C1",
                    "run-3",
                    OutcomeDecision.COMPLETED,
                    "构建脚本已创建且构建通过",
                )
            )
            return result(
                RunEvidence(
                    (successful_command("build-1", "python scripts/build.py"),)
                )
            )

        runtime.run.side_effect = complete_replacement
        service.execute_next_task(
            session.id,
            goal.id,
            "run-3",
            "执行替代任务",
        )

        def complete_final_verification(**_kwargs):
            buffers.get(goal.id, "run-4").submit_task_outcome(
                TaskOutcome(
                    "D",
                    "run-4",
                    OutcomeDecision.COMPLETED,
                    "测试和构建均通过",
                )
            )
            return result(
                RunEvidence(
                    (
                        successful_command(
                            "test-1",
                            "python -m unittest -v",
                        ),
                        successful_command(
                            "build-2",
                            "python scripts/build.py",
                        ),
                    )
                )
            )

        runtime.run.side_effect = complete_final_verification
        final = service.execute_next_task(
            session.id,
            goal.id,
            "run-4",
            "执行最终验收",
        )

        self.assertTrue(final.completion_review.accepted)
        self.assertEqual(goal.status, GoalStatus.COMPLETED)
        self.assertEqual(goal.task("C1").status, TaskStatus.COMPLETED)
        self.assertEqual(goal.task("D").status, TaskStatus.COMPLETED)
        self.assertEqual(
            [link.task_id for link in goal.run_links],
            ["C", "C", "C1", "D"],
        )


if __name__ == "__main__":
    unittest.main()
