import unittest
from unittest.mock import Mock

from core.context import BudgetAssessment, ContextManager
from core.messages import Conversation
from core.model_call import LLMCallResult, LLMResponse, ToolCall
from core.model_call.client import LLMContextLengthError
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
        self.assertIn("只返回 JSON", llm.seen_messages[0][0]["content"])
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

    def test_planning_budget_exceeded_blocks_current_run(self):
        model_calls = Mock()
        model_calls.call.return_value = ModelCallBlocked(
            BudgetAssessment(
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

    def test_first_text_triggers_reflection_second_text_completes(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="text",
                    content='{"goal": "总结", "steps": ["理解目标", "整理信息", "回答"]}',
                ),
                LLMResponse(type="text", content="初稿"),
                LLMResponse(type="text", content="复核后的最终回答"),
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

        self.assertEqual(result.answer, "复核后的最终回答")
        self.assertEqual(len(llm.seen_messages), 3)
        self.assertTrue(
            any(
                "最终回答前" in str(message.get("content"))
                for message in conversation.protocol_messages()
            )
        )

    def test_tool_failure_adds_replan_prompt_and_checkpoint_plan(self):
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
        self.assertTrue(
            any(
                "工具调用失败" in str(message.get("content"))
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
