import unittest
from unittest.mock import patch

from core.messages import Conversation
from core.model_call import InvalidLLMResponse, LLMResponse, ToolCall
from core.model_call.client import LLMTransientError
from core.runtime_modes import PlainMode
from core.tools_runtime.run_runtime import RunControl, RunRuntime, RunStatus
from core.tools_runtime.tools_protocol import validate_tool_message_chain
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


SUCCESS = {
    "ok": True,
    "code": "OK",
    "data": None,
    "error": None,
    "hint": None,
}


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def chat(self, messages, model, tools=None):
        return call_result(self.responses.pop(0))


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


class EmptyResponseLLMClient:
    def __init__(self):
        self.call_count = 0

    def chat(self, messages, model, tools=None):
        self.call_count += 1
        raise InvalidLLMResponse(
            "empty_model_response",
            "model returned empty response",
        )


class RunRuntimeStopGuardTest(unittest.TestCase):
    def test_unverified_write_cannot_complete(self):
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

        result = RunRuntime(
            model_call_service(llm),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(SUCCESS),
        ).run(
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
                for message in conversation.protocol_messages()
            )
        )


class RunRuntimeInterruptTest(unittest.TestCase):
    def test_interrupts_after_complete_tool_batch(self):
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
            context_preparation_service(),
            **runtime_tool_dependencies(SUCCESS),
        ).run(
            conversation,
            "执行工具",
            control=control,
        )

        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.final_reason, "run_interrupted")
        self.assertTrue(
            validate_tool_message_chain(conversation.protocol_messages()).ok
        )

    def test_interrupt_waits_for_verification(self):
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
            context_preparation_service(),
            **runtime_tool_dependencies(SUCCESS),
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
        self.assertTrue(
            validate_tool_message_chain(conversation.protocol_messages()).ok
        )


class RunRuntimeInvalidLLMResponseTest(unittest.TestCase):
    @patch("core.tools_runtime.run_runtime.time.sleep")
    def test_empty_response_retries_then_fails_without_conversation_pollution(
        self,
        _sleep,
    ):
        llm_client = EmptyResponseLLMClient()
        runner = RunRuntime(
            model_call_service(llm_client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )
        conversation = Conversation()

        result = runner.run(conversation, "hello")

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.final_reason, "empty_model_response")
        self.assertEqual(llm_client.call_count, 3)
        self.assertEqual(conversation.protocol_messages(), [
            {"role": "user", "content": "hello"},
        ])
        retry_checkpoints = [
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "llm_retry"
        ]
        self.assertEqual(len(retry_checkpoints), 2)

    @patch("core.tools_runtime.run_runtime.time.sleep")
    def test_empty_response_retry_can_recover(self, _sleep):
        class RecoveringLLMClient:
            def __init__(self):
                self.call_count = 0

            def chat(self, messages, model, tools=None):
                self.call_count += 1
                if self.call_count == 1:
                    raise InvalidLLMResponse(
                        "empty_model_response",
                        "model returned empty response",
                    )
                return call_result(
                    LLMResponse(type="text", content="done")
                )

        llm_client = RecoveringLLMClient()
        runner = RunRuntime(
            model_call_service(llm_client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = runner.run(Conversation(), "hello")

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(llm_client.call_count, 2)

    def test_internal_llm_client_bug_is_not_retried_or_converted(self):
        class BrokenLLMClient:
            def __init__(self):
                self.call_count = 0

            def chat(self, messages, model, tools=None):
                self.call_count += 1
                raise RuntimeError("client bug")

        llm_client = BrokenLLMClient()
        runner = RunRuntime(
            model_call_service(llm_client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        with self.assertRaisesRegex(RuntimeError, "client bug"):
            runner.run(Conversation(), "hello")

        self.assertEqual(llm_client.call_count, 1)

    @patch("core.tools_runtime.run_runtime.time.sleep")
    def test_explicit_transient_llm_error_is_retried(self, _sleep):
        class TransientLLMClient:
            def __init__(self):
                self.call_count = 0

            def chat(self, messages, model, tools=None):
                self.call_count += 1
                if self.call_count == 1:
                    raise LLMTransientError("temporary unavailable")
                return call_result(LLMResponse(type="text", content="done"))

        llm_client = TransientLLMClient()
        runner = RunRuntime(
            model_call_service(llm_client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = runner.run(Conversation(), "hello")

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(llm_client.call_count, 2)
        self.assertTrue(any(
            checkpoint.reason == "llm_retry"
            for checkpoint in result.checkpoints
        ))


if __name__ == "__main__":
    unittest.main()
