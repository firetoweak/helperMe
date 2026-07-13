import unittest

from core.messages import Conversation
from core.tools_runtime.tools_runner import ToolsRunner


class ContextLimitLLMClient:
    def chat(self, messages, model, tools=None):
        raise RuntimeError("maximum context length exceeded")


class ToolsRunnerContextTest(unittest.TestCase):
    def test_context_limit_error_terminates_without_retry(self):
        runner = ToolsRunner(ContextLimitLLMClient(), "test-model")
        conversation = Conversation()

        result = runner.run(conversation, "hello", max_rounds=3)

        self.assertEqual(result.status, "terminated")
        self.assertEqual(result.error, "context_length_exceeded")
        self.assertEqual(result.checkpoints[-1].reason, "context_length_exceeded")
        self.assertIn("上下文超过模型限制", result.answer)


if __name__ == "__main__":
    unittest.main()
