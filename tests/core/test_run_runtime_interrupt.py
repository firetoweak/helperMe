import unittest
from unittest.mock import patch

from core.context import ContextManager
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tools_runtime.tools_protocol import validate_tool_message_chain
from core.tools_runtime.run_runtime import RunControl, RunRuntime
from tests.core.llm_test_support import call_result, model_call_service


class InterruptingLLMClient:
    def __init__(self, control, responses):
        self.control = control
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, model, tools=None):
        self.calls += 1
        if self.calls == 1:
            self.control.request_interrupt("测试中断")
        return call_result(self.responses.pop(0))


SUCCESS = {
    "ok": True,
    "code": "OK",
    "data": None,
    "error": None,
    "hint": None,
}


class RunRuntimeInterruptTest(unittest.TestCase):
    @patch("core.tools_runtime.run_runtime.execute_tool", return_value=SUCCESS)
    def test_interrupts_after_complete_tool_batch(self, _execute_tool):
        control = RunControl()
        llm = InterruptingLLMClient(
            control,
            [
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("call-1", "demo", "{}")],
                )
            ],
        )
        conversation = Conversation()

        result = RunRuntime(
            model_call_service(llm),
            "test-model",
            PlainMode(),
            ContextManager(),
        ).run(
            conversation,
            "执行工具",
            control=control,
        )

        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.final_reason, "run_interrupted")
        self.assertTrue(validate_tool_message_chain(conversation.messages).ok)

    @patch("core.tools_runtime.run_runtime.execute_tool", return_value=SUCCESS)
    def test_interrupt_waits_for_verification(self, _execute_tool):
        control = RunControl()
        llm = InterruptingLLMClient(
            control,
            [
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("write-1", "write_file", "{}")],
                ),
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("verify-1", "get_changes", "{}")],
                ),
            ],
        )
        conversation = Conversation()

        result = RunRuntime(
            model_call_service(llm),
            "test-model",
            PlainMode(),
            ContextManager(),
        ).run(
            conversation,
            "修改文件",
            max_rounds=2,
            control=control,
        )

        self.assertEqual(result.status, "interrupted")
        self.assertEqual(
            [checkpoint.reason for checkpoint in result.checkpoints][-2:],
            ["tool_batch_completed", "run_interrupted"],
        )
        self.assertIn(
            "verification_required",
            [checkpoint.reason for checkpoint in result.checkpoints],
        )
        self.assertTrue(validate_tool_message_chain(conversation.messages).ok)


if __name__ == "__main__":
    unittest.main()
