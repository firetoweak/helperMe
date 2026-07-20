import unittest
from unittest.mock import Mock, patch

from core.context import ContextManager, make_budget_assessment
from core.messages import Conversation
from core.model_call import LLMCallResult, LLMResponse, ToolCall
from core.model_call.client import LLMContextLengthError, LLMTransientError
from core.model_call.service import ModelCallBlocked
from core.planning import PlanningMode
from core.runtime_modes import PlainMode
from core.tools_runtime.run_runtime import RunRuntime
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_messages = []

    def chat(self, messages, model, tools=None):
        self.seen_messages.append([message.copy() for message in messages])
        if not self.responses:
            raise RuntimeError("no more fake responses")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, LLMCallResult):
            return response
        return call_result(response)


class RunRuntimePlanningTest(unittest.TestCase):
    @staticmethod
    def _conversation() -> Conversation:
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        return conversation

    def test_runtime_plan_is_injected_without_polluting_conversation(self):
        llm = RecordingLLMClient(
            [
                call_result(
                    LLMResponse(
                        type="text",
                        content='{"goal": "分析项目", "steps": ["理解目标", "检查上下文", "总结"]}',
                    ),
                    input_tokens=101,
                    output_tokens=11,
                ),
                call_result(
                    LLMResponse(type="text", content="初稿"),
                    input_tokens=202,
                    output_tokens=22,
                ),
                call_result(
                    LLMResponse(type="text", content="最终回答"),
                    input_tokens=303,
                    output_tokens=33,
                ),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )
        conversation = self._conversation()

        result = runner.run(conversation, "帮我分析项目", max_rounds=3)

        self.assertEqual(result.status, "completed")
        self.assertIn(
            "输出必须是一个 JSON 对象",
            llm.seen_messages[0][0]["content"],
        )
        self.assertNotIn(
            "你是一个智能体助手",
            llm.seen_messages[0][0]["content"],
        )
        self.assertIn("运行时指令", llm.seen_messages[1][0]["content"])
        self.assertIn("当前执行计划", llm.seen_messages[1][0]["content"])
        self.assertIn("理解目标", llm.seen_messages[1][0]["content"])
        self.assertFalse(
            any(
                "当前运行计划" in str(message.get("content"))
                for message in conversation.protocol_messages()
            )
        )
        usage_checkpoints = [
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "llm_usage"
        ]
        self.assertEqual(
            [checkpoint.data["stage"] for checkpoint in usage_checkpoints],
            ["planning", "agent_round", "agent_round"],
        )
        self.assertEqual(
            [checkpoint.data["input_tokens"] for checkpoint in usage_checkpoints],
            [101, 202, 303],
        )
        self.assertEqual(
            [checkpoint.data["output_tokens"] for checkpoint in usage_checkpoints],
            [11, 22, 33],
        )
        created = [
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "plan_created"
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].data["plan"]["revision"], 1)
        self.assertEqual(created[0].data["plan"]["goal"], "分析项目")

    def test_planning_budget_exceeded_blocks_current_run(self):
        model_calls = Mock()
        model_calls.call.return_value = ModelCallBlocked(
            make_budget_assessment(
                estimated_input_tokens=820,
                input_budget_tokens=750,
            )
        )
        conversation = self._conversation()

        result = RunRuntime(
            model_calls,
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        ).run(conversation, "继续处理历史任务")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.final_reason, "context_budget_exceeded")
        self.assertEqual(result.checkpoints[-1].data["stage"], "planning")
        self.assertEqual(
            conversation.records[-1].payload["content"],
            "继续处理历史任务",
        )

    def test_planning_model_hard_limit_blocks_current_run(self):
        model_calls = Mock()
        model_calls.call.side_effect = LLMContextLengthError(
            "maximum context length exceeded"
        )
        conversation = self._conversation()

        result = RunRuntime(
            model_calls,
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        ).run(conversation, "继续处理历史任务")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.final_reason, "context_length_exceeded")
        self.assertEqual(result.checkpoints[-1].data["stage"], "planning")

    def test_invalid_plan_response_fails_run_without_escaping(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(type="text", content="这不是合法 Plan JSON"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = runner.run(self._conversation(), "测试非法计划")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.final_reason, "invalid_plan_response")
        self.assertIn("raw_response='这不是合法 Plan JSON'", result.answer)

    def test_first_text_triggers_reflection_second_text_completes(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="text",
                    content='{"goal": "总结", "steps": ["理解目标", "整理信息", "回答"]}',
                ),
                LLMResponse(type="text", content="初稿"),
                LLMResponse(type="text", content="这是完整且可独立阅读的最终回答"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )
        conversation = self._conversation()

        result = runner.run(conversation, "总结一下", max_rounds=3)

        self.assertEqual(result.answer, "这是完整且可独立阅读的最终回答")
        self.assertEqual(len(llm.seen_messages), 3)
        reflection_prompt = conversation.protocol_messages()[-2]["content"]
        self.assertIn("不要输出检查过程", reflection_prompt)
        self.assertIn("完整、可独立阅读", reflection_prompt)
        self.assertIn("禁止引用上一条回复", reflection_prompt)
        self.assertIn("仅输出最终答案", reflection_prompt)

        final_plan = result.checkpoints[-1].data["plan"]
        self.assertTrue(
            all(
                step["status"] in {"done", "skipped"}
                for step in final_plan["steps"]
            )
        )

    def test_tool_failure_revises_plan_before_next_agent_round(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="text",
                    content='{"goal": "测试工具失败", "steps": ["理解目标", "调用工具", "调整计划"]}',
                ),
                LLMResponse(
                    type="tool_calls",
                    calls=[
                        ToolCall(
                            id="call_1",
                            name="missing_tool_for_test",
                            arguments="{}",
                        )
                    ],
                ),
                LLMResponse(
                    type="text",
                    content=(
                        '{"action":"revise","reason":"原工具不存在",'
                        '"steps":["检查可用工具","继续任务"]}'
                    ),
                ),
                LLMResponse(type="text", content="调整后初稿"),
                LLMResponse(type="text", content="调整后最终回答"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )
        conversation = self._conversation()

        result = runner.run(conversation, "调用一个不存在的工具", max_rounds=5)

        self.assertEqual(result.status, "completed")
        self.assertIn("replanner", llm.seen_messages[2][0]["content"])
        self.assertIn("code=TOOL_NOT_FOUND", llm.seen_messages[2][0]["content"])
        self.assertIn("检查可用工具", llm.seen_messages[3][0]["content"])
        self.assertFalse(
            any(
                "如果原计划不再适用" in str(message.get("content"))
                for message in conversation.protocol_messages()
            )
        )
        tool_batch_checkpoints = [
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "tool_batch_completed"
        ]
        self.assertEqual(len(tool_batch_checkpoints), 1)
        plan_data = tool_batch_checkpoints[0].data["plan"]
        self.assertEqual(plan_data["steps"][0]["status"], "done")
        self.assertEqual(plan_data["steps"][1]["status"], "doing")

        revision_checkpoints = [
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "plan_revision_decided"
        ]
        self.assertEqual(len(revision_checkpoints), 1)
        revision = revision_checkpoints[0].data
        self.assertEqual(revision["trigger"], "tool_failure")
        self.assertEqual(revision["action"], "revise")
        self.assertEqual(revision["reason"], "原工具不存在")
        self.assertEqual(revision["before_plan"]["revision"], 1)
        self.assertEqual(revision["after_plan"]["revision"], 2)
        self.assertEqual(
            [step["text"] for step in revision["after_plan"]["steps"][-2:]],
            ["检查可用工具", "继续任务"],
        )

        usage_checkpoints = [
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "llm_usage"
        ]
        self.assertEqual(
            [checkpoint.data["stage"] for checkpoint in usage_checkpoints],
            [
                "planning",
                "agent_round",
                "replanning",
                "agent_round",
                "agent_round",
            ],
        )

    def test_tool_failure_can_keep_current_plan(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="text",
                    content=(
                        '{"goal":"测试工具失败",'
                        '"steps":["理解目标","调用工具","完成任务"]}'
                    ),
                ),
                LLMResponse(
                    type="tool_calls",
                    calls=[
                        ToolCall(
                            id="call-keep",
                            name="missing_tool_for_test",
                            arguments="{}",
                        )
                    ],
                ),
                LLMResponse(
                    type="text",
                    content=(
                        '{"action":"keep",'
                        '"reason":"失败不影响后续计划"}'
                    ),
                ),
                LLMResponse(type="text", content="初稿"),
                LLMResponse(type="text", content="最终回答"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = runner.run(self._conversation(), "测试 keep", max_rounds=4)

        revision = next(
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "plan_revision_decided"
        )
        self.assertEqual(revision.data["action"], "keep")
        self.assertEqual(revision.data["before_plan"], revision.data["after_plan"])
        self.assertEqual(revision.data["after_plan"]["revision"], 1)

    def test_invalid_replan_response_fails_run_without_escaping(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="text",
                    content=(
                        '{"goal":"测试非法 Replan",'
                        '"steps":["理解目标","调用工具"]}'
                    ),
                ),
                LLMResponse(
                    type="tool_calls",
                    calls=[
                        ToolCall(
                            id="call-invalid-replan",
                            name="missing_tool_for_test",
                            arguments="{}",
                        )
                    ],
                ),
                LLMResponse(type="text", content="这不是合法 JSON"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = runner.run(
            self._conversation(),
            "触发非法 Replan",
            max_rounds=3,
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.final_reason, "invalid_replan_response")
        self.assertIn("raw_response='这不是合法 JSON'", result.answer)

    @patch("core.tools_runtime.run_runtime.time.sleep")
    def test_planner_transient_failure_retries_in_run_runtime(self, sleep):
        llm = RecordingLLMClient(
            [
                LLMTransientError("planner timeout"),
                LLMResponse(
                    type="text",
                    content=(
                        '{"goal":"测试规划重试",'
                        '"steps":["理解目标","完成回答"]}'
                    ),
                ),
                LLMResponse(type="text", content="初稿"),
                LLMResponse(type="text", content="最终回答"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = runner.run(self._conversation(), "测试规划重试")

        self.assertEqual(result.status, "completed")
        retry = next(
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "llm_retry"
        )
        self.assertEqual(retry.data["stage"], "planning")
        self.assertIsNone(retry.data["round_index"])
        sleep.assert_called_once_with(1)

    @patch("core.tools_runtime.run_runtime.time.sleep")
    def test_replanner_transient_failure_retries_in_run_runtime(self, sleep):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="text",
                    content=(
                        '{"goal":"测试重规划重试",'
                        '"steps":["理解目标","调用工具"]}'
                    ),
                ),
                LLMResponse(
                    type="tool_calls",
                    calls=[
                        ToolCall(
                            id="call-retry",
                            name="missing_tool_for_test",
                            arguments="{}",
                        )
                    ],
                ),
                LLMTransientError("replanner timeout"),
                LLMResponse(
                    type="text",
                    content=(
                        '{"action":"keep",'
                        '"reason":"原计划仍可继续"}'
                    ),
                ),
                LLMResponse(type="text", content="初稿"),
                LLMResponse(type="text", content="最终回答"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = runner.run(self._conversation(), "测试重规划重试")

        self.assertEqual(result.status, "completed")
        retry = next(
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "llm_retry"
        )
        self.assertEqual(retry.data["stage"], "replanning")
        self.assertEqual(retry.data["round_index"], 1)
        sleep.assert_called_once_with(1)

    @patch("core.tools_runtime.run_runtime.time.sleep")
    def test_replanner_retry_exhaustion_records_stage(self, sleep):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="text",
                    content=(
                        '{"goal":"测试重规划超时",'
                        '"steps":["理解目标","调用工具"]}'
                    ),
                ),
                LLMResponse(
                    type="tool_calls",
                    calls=[
                        ToolCall(
                            id="call-timeout",
                            name="missing_tool_for_test",
                            arguments="{}",
                        )
                    ],
                ),
                LLMTransientError("replanner timeout 1"),
                LLMTransientError("replanner timeout 2"),
                LLMTransientError("replanner timeout 3"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlanningMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = runner.run(self._conversation(), "测试重规划超时")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.final_reason, "llm_error")
        self.assertEqual(result.checkpoints[-1].data["stage"], "replanning")
        self.assertEqual(result.checkpoints[-1].data["attempts"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_plain_mode_skips_plan_chain(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(type="text", content="直接最终回答"),
            ]
        )
        runner = RunRuntime(
            model_call_service(llm),
            "test-model",
            runtime_mode=PlainMode(),
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )
        conversation = self._conversation()

        result = runner.run(conversation, "直接回答", max_rounds=2)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "直接最终回答")
        self.assertEqual(len(llm.seen_messages), 1)
        self.assertFalse(
            any("当前运行计划" in str(message.get("content")) for message in llm.seen_messages[0])
        )
        self.assertFalse(
            any(
                "最终回答前" in str(message.get("content"))
                for message in conversation.protocol_messages()
            )
        )
        self.assertNotIn("plan", result.checkpoints[-1].data)

if __name__ == "__main__":
    unittest.main()
