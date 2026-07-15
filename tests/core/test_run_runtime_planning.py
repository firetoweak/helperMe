import unittest

from core.context_manager import ContextManager
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.planning import PlanningMode
from core.runtime_modes import PlainMode
from core.tools_runtime.run_runtime import RunRuntime
from tests.core.llm_test_support import call_result


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_messages = []

    def chat(self, messages, model, tools=None):
        self.seen_messages.append([message.copy() for message in messages])
        if not self.responses:
            raise RuntimeError("no more fake responses")
        return call_result(self.responses.pop(0))


class RunRuntimePlanningTest(unittest.TestCase):
    @staticmethod
    def _conversation() -> Conversation:
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        return conversation

    def test_runtime_plan_is_injected_without_polluting_conversation(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="text",
                    content='{"goal": "分析项目", "steps": ["理解目标", "检查上下文", "总结"]}',
                ),
                LLMResponse(type="text", content="初稿"),
                LLMResponse(type="text", content="最终回答"),
            ]
        )
        runner = RunRuntime(
            llm,
            "test-model",
            runtime_mode=PlanningMode(),
            context_manager=ContextManager(),
        )
        conversation = self._conversation()

        result = runner.run(conversation, "帮我分析项目", max_rounds=3)

        self.assertEqual(result.status, "completed")
        self.assertIn("只返回 JSON", llm.seen_messages[0][0]["content"])
        self.assertIn("运行时指令", llm.seen_messages[1][0]["content"])
        self.assertIn("当前执行计划", llm.seen_messages[1][0]["content"])
        self.assertIn("理解目标", llm.seen_messages[1][0]["content"])
        self.assertFalse(
            any("当前运行计划" in str(message.get("content")) for message in conversation.messages)
        )

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
            llm,
            "test-model",
            runtime_mode=PlanningMode(),
            context_manager=ContextManager(),
        )
        conversation = self._conversation()

        result = runner.run(conversation, "总结一下", max_rounds=3)

        self.assertEqual(result.answer, "复核后的最终回答")
        self.assertEqual(len(llm.seen_messages), 3)
        self.assertTrue(
            any("最终回答前" in str(message.get("content")) for message in conversation.messages)
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
            llm,
            "test-model",
            runtime_mode=PlanningMode(),
            context_manager=ContextManager(),
        )
        conversation = self._conversation()

        result = runner.run(conversation, "调用一个不存在的工具", max_rounds=5)

        self.assertEqual(result.status, "completed")
        self.assertTrue(
            any("工具调用失败" in str(message.get("content")) for message in conversation.messages)
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
            llm,
            "test-model",
            runtime_mode=PlainMode(),
            context_manager=ContextManager(),
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
            any("最终回答前" in str(message.get("content")) for message in conversation.messages)
        )
        self.assertNotIn("plan", result.checkpoints[-1].data)

if __name__ == "__main__":
    unittest.main()
