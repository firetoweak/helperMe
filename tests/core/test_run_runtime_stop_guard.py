import unittest
from unittest.mock import patch

from core.messages import Conversation, LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tools_runtime.run_runtime import RunRuntime


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, model, tools=None):
        return self.responses.pop(0)


class RunRuntimeStopGuardTest(unittest.TestCase):
    @patch(
        "core.tools_runtime.run_runtime.execute_tool",
        return_value={
            "ok": True,
            "code": "OK",
            "data": None,
            "error": None,
            "hint": None,
        },
    )
    def test_unverified_write_cannot_complete(self, _execute_tool):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("write-1", "write_file", "{}")],
                ),
                LLMResponse(type="text", content="尚未验证的回答"),
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("verify-1", "get_changes", "{}")],
                ),
                LLMResponse(type="text", content="最终回答"),
            ]
        )
        conversation = Conversation()

        result = RunRuntime(llm, "test-model", PlainMode()).run(
            conversation,
            "修改文件",
            max_rounds=4,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "最终回答")
        self.assertIsNone(result.final_reason)
        self.assertIn(
            "verification_required",
            [checkpoint.reason for checkpoint in result.checkpoints],
        )
        self.assertTrue(
            any(
                message.get("role") == "user"
                and "必须先完成验证" in str(message.get("content"))
                for message in conversation.messages
            )
        )


if __name__ == "__main__":
    unittest.main()
