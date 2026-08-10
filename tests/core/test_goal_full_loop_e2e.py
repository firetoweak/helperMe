import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.composition import create_agent_application
from core.goals import (
    CommandRequirement,
    GoalStatus,
    OutcomeDecision,
    TaskDraft,
    TaskStatus,
    TaskVerification,
    WorkspaceRequirement,
)
from core.model_call import LLMResponse, ToolCall
from core.model_call.types import LLMCallResult, LLMUsage
from core.runtime_modes import PlainMode


def tool_call(call_id: str, name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(
        calls=(ToolCall(call_id, name, json.dumps(arguments)),)
    )


def tool_batch(*calls: tuple[str, str, dict]) -> LLMResponse:
    return LLMResponse(
        calls=tuple(
            ToolCall(call_id, name, json.dumps(arguments))
            for call_id, name, arguments in calls
        )
    )


class ScriptedLLMClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses

    def chat(self, messages, model, tools=None):
        return LLMCallResult(
            response=self.responses.pop(0),
            usage=LLMUsage(input_tokens=1, output_tokens=1),
        )


class GoalFullLoopE2ETest(unittest.TestCase):
    def test_real_runtime_tools_replan_and_completion_gate_close_goal(self):
        with tempfile.TemporaryDirectory(prefix="helperme-goal-e2e-") as temp:
            root = Path(temp)
            project = root / "project"
            runtime = root / "runtime"
            project.mkdir()
            (project / "calculator.py").write_text(
                "def member_total(subtotal: float) -> float:\n"
                "    return round(subtotal * 0.8, 2)\n",
                encoding="utf-8",
            )
            (project / "test_calculator.py").write_text(
                "import unittest\n\n"
                "from calculator import member_total\n\n"
                "class CalculatorTest(unittest.TestCase):\n"
                "    def test_discount(self):\n"
                "        self.assertEqual(member_total(100), 90)\n",
                encoding="utf-8",
            )
            (project / ".gitignore").write_text(
                "__pycache__/\n*.pyc\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "e2e@example.com"],
                cwd=project,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Goal E2E"],
                cwd=project,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=project,
                check=True,
                capture_output=True,
            )

            responses = [
                tool_batch(
                    (
                        "a-test",
                        "execute_command",
                        {
                            "root": "project",
                            "command": "python -m unittest -v",
                            "workspace_effect": "read_only",
                        },
                    ),
                    (
                        "a-build",
                        "execute_command",
                        {
                            "root": "project",
                            "command": "python scripts/build.py",
                            "workspace_effect": "read_only",
                        },
                    ),
                    ("a-changes", "get_changes", {"root": "project"}),
                ),
                tool_call(
                    "a-outcome",
                    "submit_task_outcome",
                    {
                        "decision": "completed",
                        "summary": "两个问题已定位，仓库无改动",
                        "evidence": ["失败测试", "缺失构建脚本"],
                    },
                ),
                LLMResponse(content="调查完成"),
                tool_call(
                    "b-edit",
                    "apply_patch",
                    {
                        "root": "project",
                        "path": "calculator.py",
                        "old_block": "return round(subtotal * 0.8, 2)",
                        "new_block": "return round(subtotal * 0.9, 2)",
                    },
                ),
                tool_batch(
                    (
                        "b-test",
                        "execute_command",
                        {
                            "root": "project",
                            "command": "python -m unittest -v",
                            "workspace_effect": "read_only",
                        },
                    ),
                    ("b-changes", "get_changes", {"root": "project"}),
                ),
                tool_call(
                    "b-outcome",
                    "submit_task_outcome",
                    {
                        "decision": "completed",
                        "summary": "折扣修复且测试通过",
                        "evidence": ["unittest passed"],
                    },
                ),
                LLMResponse(content="问题一完成"),
                tool_call(
                    "c-outcome",
                    "submit_task_outcome",
                    {
                        "decision": "replan",
                        "summary": "scripts/build.py 不存在，无法只编辑现有文件",
                        "evidence": ["Task A 已确认文件不存在"],
                    },
                ),
                LLMResponse(content="需要重规划"),
                tool_call(
                    "plan-1",
                    "submit_plan_revision",
                    {
                        "reason": "允许创建缺失的构建脚本",
                        "replacement_tasks": [
                            {
                                "id": "C1",
                                "description": "创建 scripts/build.py 并验证构建",
                                "depends_on": ["A"],
                                "acceptance_criteria": "构建通过且改动不越界",
                                "verification": {
                                    "commands": [
                                        {
                                            "command_contains": "scripts/build.py",
                                            "root": "project",
                                            "cwd": ".",
                                            "expected_exit_codes": [0],
                                        }
                                    ],
                                    "workspace": {
                                        "root": "project",
                                        "changed": True,
                                        "allowed_paths": [
                                            "scripts/build.py",
                                        ],
                                    },
                                },
                            }
                        ],
                        "dependency_changes": [
                            {"task_id": "D", "depends_on": ["B", "C1"]}
                        ],
                    },
                ),
                LLMResponse(content="重规划完成"),
                tool_call(
                    "c1-write",
                    "write_file",
                    {
                        "root": "project",
                        "path": "scripts/build.py",
                        "content": (
                            "from pathlib import Path\n\n"
                            "source = (Path(__file__).parents[1] / "
                            "'calculator.py').read_text(encoding='utf-8')\n"
                            "assert 'subtotal * 0.9' in source\n"
                            "print('build passed')\n"
                        ),
                    },
                ),
                tool_batch(
                    (
                        "c1-build",
                        "execute_command",
                        {
                            "root": "project",
                            "command": "python scripts/build.py",
                            "workspace_effect": "read_only",
                        },
                    ),
                    ("c1-changes", "get_changes", {"root": "project"}),
                ),
                tool_call(
                    "c1-outcome",
                    "submit_task_outcome",
                    {
                        "decision": "completed",
                        "summary": "构建脚本已创建且构建通过",
                        "evidence": ["build passed"],
                    },
                ),
                LLMResponse(content="问题二完成"),
                tool_batch(
                    (
                        "d-test",
                        "execute_command",
                        {
                            "root": "project",
                            "command": "python -m unittest -v",
                            "workspace_effect": "read_only",
                        },
                    ),
                    (
                        "d-build",
                        "execute_command",
                        {
                            "root": "project",
                            "command": "python scripts/build.py",
                            "workspace_effect": "read_only",
                        },
                    ),
                    ("d-changes", "get_changes", {"root": "project"}),
                ),
                tool_call(
                    "d-outcome",
                    "submit_task_outcome",
                    {
                        "decision": "completed",
                        "summary": "测试与构建均通过",
                        "evidence": ["unittest passed", "build passed"],
                    },
                ),
                LLMResponse(content="Goal 完成"),
            ]
            llm = ScriptedLLMClient(responses)
            application = create_agent_application(
                model="test-model",
                model_context_limit=200_000,
                runtime_root=runtime,
                workspace_roots={"project": project},
                runtime_mode=PlainMode(),
                llm_client=llm,
            )
            session_id = application.create_session("session-1")
            goal = application.goals.create_goal(
                session_id,
                "goal-1",
                "修复两个问题并验证",
                [
                    TaskDraft(
                        "A",
                        "定位两个问题，不修改文件",
                        acceptance_criteria="执行失败测试和构建并保持仓库无改动",
                        verification=TaskVerification(
                            commands=(
                                CommandRequirement(
                                    "python -m unittest",
                                    expected_exit_codes=None,
                                ),
                                CommandRequirement(
                                    "scripts/build.py",
                                    expected_exit_codes=None,
                                ),
                            ),
                            workspace=WorkspaceRequirement(
                                "project",
                                changed=False,
                            ),
                        ),
                    ),
                    TaskDraft(
                        "B",
                        "修复折扣",
                        depends_on=("A",),
                        acceptance_criteria="测试通过且只改 calculator.py",
                        verification=TaskVerification(
                            commands=(CommandRequirement("python -m unittest"),),
                            workspace=WorkspaceRequirement(
                                "project",
                                changed=True,
                                allowed_paths=("calculator.py",),
                            ),
                        ),
                    ),
                    TaskDraft(
                        "C",
                        "只编辑现有 scripts/build.py",
                        depends_on=("A",),
                        acceptance_criteria="不存在时必须重规划",
                    ),
                    TaskDraft(
                        "D",
                        "整体验证",
                        depends_on=("B", "C"),
                        acceptance_criteria="测试和构建均通过",
                        verification=TaskVerification(
                            commands=(
                                CommandRequirement("python -m unittest"),
                                CommandRequirement("scripts/build.py"),
                            ),
                            workspace=WorkspaceRequirement(
                                "project",
                                changed=True,
                                allowed_paths=(
                                    "calculator.py",
                                    "scripts/build.py",
                                ),
                            ),
                        ),
                    ),
                ],
            )

            first = application.goals.execute_next_task(
                session_id,
                goal.id,
                "run-1",
                "执行当前 Task",
            )
            self.assertIsNotNone(first.applied_outcome)
            self.assertEqual(
                first.applied_outcome.decision,
                OutcomeDecision.COMPLETED,
                msg={
                    "evidence": [
                        (step.name, step.arguments, step.result)
                        for step in first.session_outcome.result.evidence.steps
                    ],
                    "messages": application._session_runtime
                    .get_session(session_id)
                    .conversation.protocol_messages(),
                },
            )
            self.assertEqual(goal.status, GoalStatus.ACTIVE)
            self.assertEqual(goal.next_task().id, "B")
            self.assertEqual(len(llm.responses), 15)
            second = application.goals.execute_next_task(
                session_id,
                goal.id,
                "run-2",
                "执行当前 Task",
            )
            self.assertIsNotNone(second.applied_outcome)
            self.assertEqual(len(llm.responses), 11)
            third = application.goals.execute_next_task(
                session_id,
                goal.id,
                "run-3",
                "执行当前 Task",
            )
            self.assertIsNotNone(third.applied_outcome)
            self.assertEqual(len(llm.responses), 9)
            revision = application.goals.execute_plan_revision(
                session_id,
                goal.id,
                "run-4",
                "重规划",
            )
            self.assertIsNotNone(revision.applied_revision)
            self.assertEqual(len(llm.responses), 7)
            replacement = application.goals.execute_next_task(
                session_id,
                goal.id,
                "run-5",
                "执行替代 Task",
            )
            self.assertIsNotNone(replacement.applied_outcome)
            self.assertEqual(len(llm.responses), 3)
            final = application.goals.execute_next_task(
                session_id,
                goal.id,
                "run-6",
                "执行最终验证",
            )

            self.assertEqual(llm.responses, [])
            self.assertTrue(final.completion_review.accepted)
            self.assertEqual(goal.status, GoalStatus.COMPLETED)
            self.assertEqual(goal.task("C").status, TaskStatus.SUPERSEDED)
            self.assertEqual(
                [outcome.decision for outcome in goal.outcomes],
                [
                    OutcomeDecision.COMPLETED,
                    OutcomeDecision.COMPLETED,
                    OutcomeDecision.REPLAN,
                    OutcomeDecision.COMPLETED,
                    OutcomeDecision.COMPLETED,
                ],
            )
            self.assertEqual(
                subprocess.run(
                    ["python", "-m", "unittest", "-v"],
                    cwd=project,
                    check=False,
                ).returncode,
                0,
            )
            self.assertEqual(
                subprocess.run(
                    ["python", "scripts/build.py"],
                    cwd=project,
                    check=False,
                ).returncode,
                0,
            )


if __name__ == "__main__":
    unittest.main()
