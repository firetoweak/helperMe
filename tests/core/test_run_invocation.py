import json
import unittest

from pydantic import BaseModel

from core.goals import (
    GoalCommandBuffer,
    GoalApplicationService,
    GoalCommandBufferRegistry,
    GoalCommandKind,
    Goal,
    GoalPlanRevisionCapability,
    GoalTaskCapability,
    GoalStatus,
    InMemoryGoalStore,
    OutcomeDecision,
    SUBMIT_PLAN_REVISION,
    SUBMIT_TASK_OUTCOME,
    Task,
    TaskDraft,
    TaskOutcome,
    TaskStatus,
    CommandRequirement,
    TaskVerification,
)
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.session import SessionRuntime
from core.tools_runtime import RunInvocation
from core.tools_runtime.run_runtime import RunRuntime, RunStatus
from core.tool_registry import ToolSpec
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def chat(self, messages, model, tools=None):
        self.requests.append(
            {
                "messages": messages,
                "tools": tools or [],
            }
        )
        return call_result(self.responses.pop(0))


class FakeCommandInput(BaseModel):
    root: str
    cwd: str = "."
    command: str
    workspace_effect: str = "read_only"


class FakeWriteInput(BaseModel):
    path: str


class RunInvocationTest(unittest.TestCase):
    @staticmethod
    def capability(buffer: GoalCommandBuffer) -> GoalTaskCapability:
        return GoalTaskCapability(
            goal_id="goal-1",
            objective="完成目标",
            task=Task(
                id="A",
                description="执行当前任务",
                depends_on=(),
                acceptance_criteria=None,
                status=TaskStatus.ACTIVE,
            ),
            command_buffer=buffer,
        )

    @staticmethod
    def runner(client: RecordingLLMClient) -> RunRuntime:
        return RunRuntime(
            model_call_service(client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

    @staticmethod
    def conversation() -> Conversation:
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        return conversation

    def test_capability_injects_instruction_tool_and_completion_barrier(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-1",
            expected_kind=GoalCommandKind.TASK_OUTCOME,
        )
        client = RecordingLLMClient(
            [
                LLMResponse(content="我认为任务已经完成"),
                LLMResponse(
                    calls=(
                        ToolCall(
                            "call-1",
                            SUBMIT_TASK_OUTCOME,
                            json.dumps(
                                {
                                    "decision": "completed",
                                    "summary": "测试通过",
                                    "evidence": ["274 tests passed"],
                                }
                            ),
                        ),
                    )
                ),
                LLMResponse(content="任务已完成"),
            ]
        )

        result = self.runner(client).run(
            self.conversation(),
            "执行任务",
            invocation=RunInvocation((self.capability(buffer),)),
        )

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(
            buffer.task_outcome.decision,
            OutcomeDecision.COMPLETED,
        )
        self.assertEqual(len(client.requests), 3)
        feedback = client.requests[1]["messages"][-1]["content"]
        self.assertIn("submit_task_outcome", feedback)
        runtime_prompts = [
            checkpoint.data["runtime_prompts"]
            for checkpoint in result.checkpoints
            if checkpoint.reason == "llm_request"
            and checkpoint.data["stage"] == "agent_round"
        ]
        self.assertTrue(
            all("Goal ID：goal-1" in prompts[-1] for prompts in runtime_prompts)
        )
        self.assertEqual(
            result.checkpoints[-1].data["goal_command"]["decision"],
            "completed",
        )

    def test_completion_gate_keeps_run_open_until_real_command_passes(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-1",
            expected_kind=GoalCommandKind.TASK_OUTCOME,
        )
        capability = GoalTaskCapability(
            goal_id="goal-1",
            objective="完成测试",
            task=Task(
                id="A",
                description="运行测试",
                depends_on=(),
                acceptance_criteria="测试通过",
                verification=TaskVerification(
                    commands=(CommandRequirement("python -m unittest"),)
                ),
                status=TaskStatus.ACTIVE,
            ),
            command_buffer=buffer,
        )
        client = RecordingLLMClient(
            [
                LLMResponse(
                    calls=(
                        ToolCall(
                            "outcome-1",
                            SUBMIT_TASK_OUTCOME,
                            json.dumps(
                                {
                                    "decision": "completed",
                                    "summary": "测试通过",
                                    "evidence": ["模型自由文本不能作为证据"],
                                }
                            ),
                        ),
                    )
                ),
                LLMResponse(content="任务完成"),
                LLMResponse(
                    calls=(
                        ToolCall(
                            "command-1",
                            "execute_command",
                            json.dumps(
                                {
                                    "root": "workspace",
                                    "cwd": ".",
                                    "command": "python -m unittest -v",
                                    "workspace_effect": "read_only",
                                }
                            ),
                        ),
                    )
                ),
                LLMResponse(content="验收完成"),
            ]
        )
        dependencies = runtime_tool_dependencies()
        dependencies["tools_executor"].registry.register(
            ToolSpec(
                name="execute_command",
                description="执行测试命令",
                input_model=FakeCommandInput,
                handler=lambda _input: {
                    "ok": True,
                    "code": "COMMAND_COMPLETED",
                    "data": {
                        "exit_code": 0,
                        "timed_out": False,
                        "workspace_effect": "read_only",
                    },
                },
            )
        )
        runner = RunRuntime(
            model_call_service(client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **dependencies,
        )

        result = runner.run(
            self.conversation(),
            "运行测试",
            invocation=RunInvocation((capability,)),
        )

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(len(client.requests), 4)
        feedback = client.requests[2]["messages"][-1]["content"]
        self.assertIn("缺少命令验收证据", feedback)
        self.assertEqual(
            [step.name for step in result.evidence.steps],
            [SUBMIT_TASK_OUTCOME, "execute_command"],
        )

    def test_capability_tool_is_scoped_to_one_run(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-1",
            expected_kind=GoalCommandKind.TASK_OUTCOME,
        )
        first_client = RecordingLLMClient(
            [
                LLMResponse(
                    calls=(
                        ToolCall(
                            "call-1",
                            SUBMIT_TASK_OUTCOME,
                            json.dumps(
                                {
                                    "decision": "continue",
                                    "summary": "需要继续",
                                    "evidence": [],
                                }
                            ),
                        ),
                    )
                ),
                LLMResponse(content="本轮结束"),
            ]
        )
        runner = self.runner(first_client)
        runner.run(
            self.conversation(),
            "执行 Goal",
            invocation=RunInvocation((self.capability(buffer),)),
        )

        second_client = RecordingLLMClient([LLMResponse(content="普通回答")])
        runner.model_calls = model_call_service(second_client)
        result = runner.run(self.conversation(), "普通问题")

        first_names = {
            tool["function"]["name"]
            for tool in first_client.requests[0]["tools"]
        }
        second_names = {
            tool["function"]["name"]
            for tool in second_client.requests[0]["tools"]
        }
        self.assertIn(SUBMIT_TASK_OUTCOME, first_names)
        self.assertNotIn(SUBMIT_TASK_OUTCOME, second_names)
        self.assertEqual(result.status, RunStatus.COMPLETED)

    def test_bound_tool_schema_does_not_expose_association_ids(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-1",
            expected_kind=GoalCommandKind.TASK_OUTCOME,
        )

        schema = self.capability(buffer).tool_specs()[0].to_openai_tool()
        properties = schema["function"]["parameters"]["properties"]

        self.assertEqual(
            set(properties),
            {"decision", "summary", "evidence"},
        )
        self.assertTrue(
            {"goal_id", "task_id", "run_id"}.isdisjoint(properties)
        )

    def test_plan_revision_capability_buffers_explicit_revision(self):
        buffer = GoalCommandBuffer(
            goal_id="goal-1",
            task_id="A",
            run_id="run-2",
            expected_kind=GoalCommandKind.PLAN_REVISION,
        )
        goal = Goal(
            "goal-1",
            "完成目标",
            [TaskDraft("A", "原任务")],
        )
        goal.start_task_run("A", "run-1")
        goal.finish_task_run("run-1")
        goal.record_outcome(
            TaskOutcome(
                task_id="A",
                run_id="run-1",
                decision=OutcomeDecision.REPLAN,
                summary="需要拆分任务",
            )
        )
        capability = GoalPlanRevisionCapability(
            goal=goal,
            task_id="A",
            command_buffer=buffer,
        )
        client = RecordingLLMClient(
            [
                LLMResponse(
                    calls=(
                        ToolCall(
                            "call-0",
                            "write_file",
                            json.dumps({"path": "should-not-exist.txt"}),
                        ),
                    )
                ),
                LLMResponse(
                    calls=(
                        ToolCall(
                            "call-1",
                            SUBMIT_PLAN_REVISION,
                            json.dumps(
                                {
                                    "reason": "无效循环方案",
                                    "replacement_tasks": [
                                        {
                                            "id": "A1",
                                            "description": "新任务",
                                            "depends_on": ["A1"],
                                            "acceptance_criteria": None,
                                        }
                                    ],
                                    "dependency_changes": [],
                                }
                            ),
                        ),
                    )
                ),
                LLMResponse(
                    calls=(
                        ToolCall(
                            "call-2",
                            SUBMIT_PLAN_REVISION,
                            json.dumps(
                                {
                                    "reason": "拆分执行步骤",
                                    "replacement_tasks": [
                                        {
                                            "id": "A1",
                                            "description": "新任务",
                                            "depends_on": [],
                                            "acceptance_criteria": None,
                                        }
                                    ],
                                    "dependency_changes": [],
                                }
                            ),
                        ),
                    )
                ),
                LLMResponse(content="重规划完成"),
            ]
        )

        dependencies = runtime_tool_dependencies()
        dependencies["tools_executor"].registry.register(
            ToolSpec(
                name="write_file",
                description="写文件",
                input_model=FakeWriteInput,
                handler=lambda _input: self.fail("重规划 Run 不得执行基础工具"),
            )
        )
        runner = RunRuntime(
            model_call_service(client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **dependencies,
        )

        result = runner.run(
            self.conversation(),
            "重新规划",
            invocation=RunInvocation((capability,)),
        )

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(buffer.plan_revision.task_id, "A")
        self.assertEqual(buffer.plan_revision.replacement_tasks[0].id, "A1")
        self.assertEqual(result.evidence.steps[0].result["code"], "TOOL_NOT_FOUND")
        exposed_names = {
            tool["function"]["name"] for tool in client.requests[0]["tools"]
        }
        self.assertNotIn("write_file", exposed_names)

    def test_goal_service_to_run_runtime_commits_a_bound_model_command(self):
        client = RecordingLLMClient(
            [
                LLMResponse(
                    calls=(
                        ToolCall(
                            "call-1",
                            SUBMIT_TASK_OUTCOME,
                            json.dumps(
                                {
                                    "decision": "completed",
                                    "summary": "验收通过",
                                    "evidence": ["tests passed"],
                                }
                            ),
                        ),
                    )
                ),
                LLMResponse(content="任务完成"),
            ]
        )
        sessions = SessionRuntime(run_runtime=self.runner(client))
        sessions.create_session("session-1", "system prompt")
        service = GoalApplicationService(
            sessions,
            InMemoryGoalStore(),
            GoalCommandBufferRegistry(),
        )
        goal = service.create_goal(
            "session-1",
            "goal-1",
            "完成目标",
            [TaskDraft("A", "执行任务")],
        )

        result = service.execute_next_task(
            "session-1",
            "goal-1",
            "run-1",
            "执行任务",
        )

        self.assertEqual(result.session_outcome.result.status, RunStatus.COMPLETED)
        self.assertEqual(result.applied_outcome.decision, OutcomeDecision.COMPLETED)
        self.assertEqual(goal.status, GoalStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
