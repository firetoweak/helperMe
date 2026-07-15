import unittest
from unittest.mock import Mock, patch

from core.context_manager import ContextManager
from core.messages import Conversation
from core.model_call import LLMResponse
from core.model_call.client import LLMContextLengthError, LLMTransientError
from core.runtime_modes import PlainMode
from core.tools_runtime.run_runtime import RunRuntime, RunStatus
from tests.core.llm_test_support import call_result


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def chat(self, messages, model, tools=None):
        self.messages.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return call_result(response)


class ContextLimitLLMClient:
    def chat(self, messages, model, tools=None):
        raise LLMContextLengthError("maximum context length exceeded")


class ChangingInstructionsMode:
    def __init__(self):
        self.instruction = "第一轮指令"
        self.instruction_calls = 0

    def start(self, user_message, conversation, llm_client, model):
        return None

    def runtime_instructions(self):
        self.instruction_calls += 1
        return [self.instruction]

    def on_assistant_text(self, conversation):
        if self.instruction == "第一轮指令":
            self.instruction = "第二轮指令"
            return True
        return False

    def after_tool_batch(self, conversation, tools_state, batch_steps):
        return None

    def checkpoint_data(self):
        return None


class StaticInstructionsMode(ChangingInstructionsMode):
    def on_assistant_text(self, conversation):
        return False


class RunRuntimeContextTest(unittest.TestCase):
    def test_context_limit_error_blocks_without_retry(self):
        runner = RunRuntime(
            ContextLimitLLMClient(),
            "test-model",
            PlainMode(),
            ContextManager(),
        )
        conversation = Conversation()

        result = runner.run(conversation, "hello", max_rounds=3)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.final_reason, "context_length_exceeded")
        self.assertEqual(result.checkpoints[-1].kind, "run")
        self.assertEqual(result.checkpoints[-1].reason, "context_length_exceeded")
        self.assertIn("上下文超过模型限制", result.answer)

    @patch("core.tools_runtime.run_runtime.time.sleep")
    def test_retry_reuses_one_model_context_snapshot(self, _sleep):
        llm_client = RecordingLLMClient(
            [
                LLMTransientError("temporary unavailable"),
                LLMResponse(type="text", content="done"),
            ]
        )
        mode = StaticInstructionsMode()
        context_manager = Mock(wraps=ContextManager())
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")

        result = RunRuntime(
            llm_client=llm_client,
            model="test-model",
            runtime_mode=mode,
            context_manager=context_manager,
        ).run(conversation, "hello")

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(context_manager.build.call_count, 1)
        self.assertEqual(mode.instruction_calls, 1)
        self.assertIs(llm_client.messages[0], llm_client.messages[1])
        self.assertIn("第一轮指令", llm_client.messages[0][0]["content"])
        self.assertEqual(conversation.messages[0]["content"], "system prompt")

    def test_each_round_builds_a_snapshot_with_current_instructions(self):
        llm_client = RecordingLLMClient(
            [
                LLMResponse(type="text", content="draft"),
                LLMResponse(type="text", content="done"),
            ]
        )
        mode = ChangingInstructionsMode()
        context_manager = Mock(wraps=ContextManager())
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")

        result = RunRuntime(
            llm_client=llm_client,
            model="test-model",
            runtime_mode=mode,
            context_manager=context_manager,
        ).run(conversation, "hello")

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(context_manager.build.call_count, 2)
        self.assertEqual(mode.instruction_calls, 2)
        self.assertIn("第一轮指令", llm_client.messages[0][0]["content"])
        self.assertIn("第二轮指令", llm_client.messages[1][0]["content"])


if __name__ == "__main__":
    unittest.main()
