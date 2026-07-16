import unittest
from unittest.mock import patch

from core.context import ContextManager
from core.messages import Conversation
from core.model_call import InvalidLLMResponse, LLMResponse
from core.model_call.client import LLMTransientError
from core.runtime_modes import PlainMode
from core.tools_runtime.run_runtime import RunRuntime, RunStatus
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


class EmptyResponseLLMClient:
    def __init__(self):
        self.call_count = 0

    def chat(self, messages, model, tools=None):
        self.call_count += 1
        raise InvalidLLMResponse(
            "empty_model_response",
            "model returned empty response",
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
