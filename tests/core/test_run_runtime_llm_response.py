import unittest

from core.messages import Conversation, InvalidLLMResponse
from core.tools_runtime.run_runtime import RunRuntime, RunStatus


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
    def test_empty_response_fails_without_retry_or_conversation_pollution(self):
        llm_client = EmptyResponseLLMClient()
        runner = RunRuntime(llm_client, "test-model")
        conversation = Conversation()

        result = runner.run(conversation, "hello")

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.final_reason, "empty_model_response")
        self.assertEqual(llm_client.call_count, 1)
        self.assertEqual(conversation.messages, [
            {"role": "user", "content": "hello"},
        ])
        self.assertFalse(any(
            checkpoint.reason == "llm_retry"
            for checkpoint in result.checkpoints
        ))


if __name__ == "__main__":
    unittest.main()
