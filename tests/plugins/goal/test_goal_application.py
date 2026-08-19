import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from core.composition import create_agent_application
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tools_runtime.turn_evidence import TurnEvidence
from core.tools_runtime.turn_runtime import TurnStatus
from core.tools_runtime.turn_invocation import TurnInvocation
from plugins.goal.application import GoalApplicationService
from plugins.goal.capabilities import (
    ContractCompilationCapability,
    GoalExecutorCapability,
    GoalJudgeCapability,
)
from plugins.goal.submissions import JudgmentSubmission
from plugins.goal.goal import (
    CompletionContractDraft,
    CompletionCriterion,
    CriterionAuthority,
    GoalStatus,
    JudgmentDecision,
)
from plugins.goal.store import InMemoryGoalStore
from tests.core.llm_test_support import call_result


def user_criterion():
    return CompletionCriterion(
        "user-goal",
        "完成用户目标",
        CriterionAuthority.USER,
        ("Judge 独立检查最终状态",),
    )


def outcome(status=TurnStatus.COMPLETED, answer="完成", reason=None):
    return SimpleNamespace(
        result=SimpleNamespace(
            status=status,
            answer=answer,
            final_reason=reason,
            evidence=TurnEvidence(),
        )
    )


class FakeTurnHost:
    def __init__(self, judgments):
        self.sessions = {"session-1"}
        self.used_turns = set()
        self.judgments = iter(judgments)
        self.executions = []
        self.invocations = []
        self.deleted_sessions = []

    def create_session(self, session_id):
        self.sessions.add(session_id)
        return session_id

    def delete_session(self, session_id):
        self.sessions.remove(session_id)
        self.deleted_sessions.append(session_id)

    def require_session(self, session_id):
        if session_id not in self.sessions:
            raise KeyError(session_id)

    def validate_turn(self, session_id, turn_id, user_message):
        self.require_session(session_id)
        if not turn_id.strip() or not user_message.strip():
            raise ValueError("invalid turn")
        if (session_id, turn_id) in self.used_turns:
            raise ValueError("duplicate turn")

    def request_interrupt(self, session_id, reason=None):
        raise AssertionError("test did not expect an interrupt")

    async def execute(
        self,
        session_id,
        turn_id,
        user_message,
        max_steps,
        invocation,
    ):
        self.validate_turn(session_id, turn_id, user_message)
        self.used_turns.add((session_id, turn_id))
        capability = invocation.capabilities[0]
        self.invocations.append(invocation)
        self.executions.append((session_id, user_message, capability))

        if isinstance(capability, ContractCompilationCapability):
            capability.buffer.submit(
                CompletionContractDraft((user_criterion(),))
            )
            return outcome(answer="Contract 已编译")
        if isinstance(capability, GoalExecutorCapability):
            return outcome(answer=f"Executor Turn {capability.turn_index}")
        if isinstance(capability, GoalJudgeCapability):
            capability.buffer.submit(next(self.judgments))
            return outcome(answer="Judge 已验收")
        raise AssertionError(type(capability))


class GoalApplicationServiceTest(unittest.IsolatedAsyncioTestCase):
    def ids(self):
        index = 0

        def next_id():
            nonlocal index
            index += 1
            return str(index)

        return next_id

    def service(self, host, max_turns=3):
        return GoalApplicationService(
            host,
            InMemoryGoalStore(),
            default_max_turns=max_turns,
            id_factory=self.ids(),
        )

    async def test_turns_executor_then_independent_judge_until_done(self):
        host = FakeTurnHost(
            [
                JudgmentSubmission(
                    JudgmentDecision.CONTINUE,
                    "仍缺完整验证",
                    ("只完成局部检查",),
                ),
                JudgmentSubmission(
                    JudgmentDecision.DONE,
                    "目标已经完成",
                    ("独立验证通过",),
                ),
            ]
        )
        result = await self.service(host).start_goal(
            "session-1",
            "goal-1",
            "executor-1",
            "修复并验证问题",
        )

        self.assertEqual(result.goal.status, GoalStatus.COMPLETED)
        self.assertEqual(result.goal.turn_count, 2)
        self.assertEqual(len(result.turns), 2)

        executor_calls = [
            item
            for item in host.executions
            if isinstance(item[2], GoalExecutorCapability)
        ]
        judge_calls = [
            item
            for item in host.executions
            if isinstance(item[2], GoalJudgeCapability)
        ]
        self.assertEqual([item[0] for item in executor_calls], ["session-1"] * 2)
        self.assertTrue(all(item[0] != "session-1" for item in judge_calls))
        self.assertIn("仍缺完整验证", executor_calls[1][1])
        self.assertTrue(all(session not in host.sessions for session in host.deleted_sessions))

    async def test_executor_and_judge_receive_turn_skill_provider_and_reload_per_turn(self):
        host = FakeTurnHost([
            JudgmentSubmission(
                JudgmentDecision.DONE,
                "done",
                ("verified",),
            ),
        ])
        skill_provider = object()

        await self.service(host).start_goal(
            "session-1",
            "goal-1",
            "executor-1",
            "use skill",
            invocation=TurnInvocation(skill_provider=skill_provider),
        )

        self.assertIsNone(host.invocations[0].skill_provider)
        self.assertIs(host.invocations[1].skill_provider, skill_provider)
        self.assertIs(host.invocations[2].skill_provider, skill_provider)
        self.assertIsInstance(
            host.invocations[1].capabilities[0],
            GoalExecutorCapability,
        )
        self.assertIsInstance(
            host.invocations[2].capabilities[0],
            GoalJudgeCapability,
        )

    async def test_continue_at_max_turns_becomes_exhausted(self):
        host = FakeTurnHost(
            [
                JudgmentSubmission(
                    JudgmentDecision.CONTINUE,
                    "尚未完成",
                    (),
                )
            ]
        )

        result = await self.service(host, max_turns=1).start_goal(
            "session-1",
            "goal-1",
            "executor-1",
            "完成目标",
        )

        self.assertEqual(result.goal.status, GoalStatus.EXHAUSTED)
        self.assertEqual(result.goal.turn_count, 1)

    async def test_contract_revision_only_affects_next_executor_turn(self):
        replacement = CompletionContractDraft(
            (
                user_criterion(),
                CompletionCriterion(
                    "inferred-check",
                    "补充独立检查",
                    CriterionAuthority.INFERRED,
                    ("检查结果",),
                ),
            )
        )
        host = FakeTurnHost(
            [
                JudgmentSubmission(
                    JudgmentDecision.CONTINUE,
                    "需要补充检查",
                    (),
                    contract_revision=replacement,
                    revision_reason="发现需要更明确的推导标准",
                ),
                JudgmentSubmission(
                    JudgmentDecision.DONE,
                    "完成",
                    ("检查通过",),
                ),
            ]
        )

        result = await self.service(host).start_goal(
            "session-1",
            "goal-1",
            "executor-1",
            "完成目标",
        )

        executor_contract_versions = [
            capability.contract.version
            for _, _, capability in host.executions
            if isinstance(capability, GoalExecutorCapability)
        ]
        self.assertEqual(executor_contract_versions, [1, 2])
        self.assertEqual(len(result.goal.contract_revisions), 1)

    async def test_executor_exception_is_preserved_and_goal_is_paused(self):
        class ExecutorBug(RuntimeError):
            pass

        host = FakeTurnHost([])
        execute = host.execute

        async def fail_executor(*args, **kwargs):
            invocation = args[-1] if args else kwargs["invocation"]
            if isinstance(
                invocation.capabilities[0],
                GoalExecutorCapability,
            ):
                raise ExecutorBug("original bug")
            return await execute(*args, **kwargs)

        host.execute = fail_executor
        store = InMemoryGoalStore()
        service = GoalApplicationService(
            host,
            store,
            default_max_turns=3,
            id_factory=self.ids(),
        )

        with self.assertRaisesRegex(ExecutorBug, "original bug"):
            await service.start_goal(
                "session-1",
                "goal-1",
                "executor-1",
                "完成目标",
            )

        self.assertEqual(
            store.get("goal-1").status,
            GoalStatus.PAUSED,
        )


class GoalLoopRuntimeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_contract_executor_and_independent_judge_turn_through_runtime(self):
        llm = Mock()
        llm.chat = AsyncMock()
        llm.chat.side_effect = [
            call_result(
                LLMResponse(
                    calls=(
                        ToolCall(
                            "contract-call",
                            "submit_completion_contract",
                            json.dumps(
                                {
                                    "criteria": [
                                        {
                                            "id": "user-goal",
                                            "description": "完成用户目标",
                                            "authority": "user",
                                            "evidence_requirements": [
                                                "Judge 独立检查最终结果"
                                            ],
                                        }
                                    ],
                                    "verification": {
                                        "commands": [],
                                        "workspace": None,
                                    },
                                }
                            ),
                        ),
                    )
                )
            ),
            call_result(LLMResponse(content="Contract 已冻结")),
            call_result(LLMResponse(content="Executor 已完成目标")),
            call_result(
                LLMResponse(
                    calls=(
                        ToolCall(
                            "judge-call",
                            "submit_goal_judgment",
                            json.dumps(
                                {
                                    "decision": "done",
                                    "reason": "独立检查确认完成",
                                    "evidence": ["读取最终结果并核对目标"],
                                    "revised_contract": None,
                                    "revision_reason": None,
                                }
                            ),
                        ),
                    )
                )
            ),
            call_result(LLMResponse(content="Judge 验收通过")),
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            runtime = root / "runtime"
            project.mkdir()
            application = create_agent_application(
                "executor-model",
                model_context_limit=100_000,
                agent_workspace=AgentWorkspace(runtime),
                workspace_roots={"project": project},
                runtime_mode=PlainMode(),
                llm_client=llm,
            )
            await application.__aenter__()
            self.addAsyncCleanup(application.close)
            application.create_session("session-1")
            service = GoalApplicationService(
                application,
                InMemoryGoalStore(),
                default_max_turns=3,
            )

            result = await service.start_goal(
                "session-1",
                "goal-1",
                "executor-turn-1",
                "完成目标",
            )

        self.assertEqual(result.goal.status, GoalStatus.COMPLETED)
        self.assertEqual(
            [call.args[1] for call in llm.chat.call_args_list],
            [
                "executor-model",
                "executor-model",
                "executor-model",
                "executor-model",
                "executor-model",
            ],
        )
        judge_tools = {
            item["function"]["name"]
            for item in llm.chat.call_args_list[3].args[2]
        }
        self.assertIn("execute_command", judge_tools)
        self.assertIn("submit_goal_judgment", judge_tools)
        self.assertTrue(
            {"write_file", "apply_patch", "replace_all"}.isdisjoint(
                judge_tools
            )
        )


if __name__ == "__main__":
    unittest.main()
from core.agent_workspace import AgentWorkspace
