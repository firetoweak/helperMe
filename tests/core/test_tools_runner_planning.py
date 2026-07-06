import unittest

from core.messages import Conversation, LLMResponse, ToolCall
from core.tools_runner import ToolsRunner


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_messages = []

    def chat(self, messages, model, tools=None):
        self.seen_messages.append([message.copy() for message in messages])
        if not self.responses:
            raise RuntimeError("no more fake responses")
        return self.responses.pop(0)


class ToolsRunnerPlanningTest(unittest.TestCase):
    def test_runtime_plan_is_injected_without_polluting_conversation(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(type="text", content="初稿"),
                LLMResponse(type="text", content="最终回答"),
            ]
        )
        runner = ToolsRunner(llm, "test-model")
        conversation = Conversation()

        result = runner.run(conversation, "帮我分析项目", max_rounds=3)

        self.assertEqual(result.status, "completed")
        self.assertIn("当前运行计划", llm.seen_messages[0][0]["content"])
        self.assertFalse(
            any("当前运行计划" in str(message.get("content")) for message in conversation.messages)
        )

    def test_first_text_triggers_reflection_second_text_completes(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(type="text", content="初稿"),
                LLMResponse(type="text", content="复核后的最终回答"),
            ]
        )
        runner = ToolsRunner(llm, "test-model")
        conversation = Conversation()

        result = runner.run(conversation, "总结一下", max_rounds=3)

        self.assertEqual(result.answer, "复核后的最终回答")
        self.assertEqual(len(llm.seen_messages), 2)
        self.assertTrue(
            any("最终回答前" in str(message.get("content")) for message in conversation.messages)
        )

    def test_tool_failure_adds_replan_prompt_and_checkpoint_plan(self):
        llm = RecordingLLMClient(
            [
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
        runner = ToolsRunner(llm, "test-model")
        conversation = Conversation()

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


if __name__ == "__main__":
    unittest.main()
