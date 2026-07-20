import unittest
from unittest.mock import Mock, patch

from core.context import (
    ContextManager,
    ContextState,
    MicroCompactionDecision,
    ModelContext,
    PreparedContext,
    SummaryCompaction,
    SummaryGeneration,
    empty_tool_window_stats,
    make_budget_assessment,
)
from core.messages import Conversation
from core.model_call import LLMResponse
from core.model_call.client import LLMContextLengthError, LLMTransientError
from core.model_call.service import ModelCallBlocked
from core.runtime_modes import PlainMode
from core.tools_runtime.run_runtime import RunRuntime, RunStatus
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


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

    def start(self, conversation):
        return None

    def accept_start_response(self, response):
        raise AssertionError("no start model call")

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

    def handle_tool_failures(self, conversation, failed_steps):
        return None

    def accept_tool_failure_response(self, response):
        raise AssertionError("no replanning model call")

    def checkpoint_data(self):
        return None


class StaticInstructionsMode(ChangingInstructionsMode):
    def on_assistant_text(self, conversation):
        return False


class RunRuntimeContextTest(unittest.TestCase):
    def test_level2_records_checkpoint_and_notifies_user(self):
        before = make_budget_assessment(900, 750)
        after = make_budget_assessment(500, 750)
        state = ContextState(
            summary="handoff",
            summarized_through_message_id="old-message",
        )
        decision = MicroCompactionDecision(
            candidate_state=ContextState(),
            before=before,
            after=before,
            changed=False,
            tool_window=empty_tool_window_stats(),
        )
        context_preparation = Mock()
        context_preparation.prepare.return_value = PreparedContext(
            model_context=ModelContext(
                messages=[{"role": "user", "content": "hello"}]
            ),
            context_state=state,
            micro_compaction=decision,
            composition=after.composition,
            summary_compaction=SummaryCompaction(
                boundary_message_id="old-message",
                before=before,
                after=after,
                generation=SummaryGeneration("handoff", 300, 20),
            ),
        )
        model_calls = Mock()
        model_calls.call.return_value = call_result(
            LLMResponse(type="text", content="done")
        )

        result = RunRuntime(
            model_calls,
            "test-model",
            PlainMode(),
            context_preparation,
            **runtime_tool_dependencies(),
        ).run(Conversation(), "hello")

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.context_state, state)
        self.assertTrue(result.answer.startswith("本轮已执行上下文压缩。"))
        self.assertIn(
            "level2_context_compressed",
            [checkpoint.reason for checkpoint in result.checkpoints],
        )

    def test_project_budget_exceeded_blocks_before_model_call(self):
        model_calls = Mock()
        model_calls.call.return_value = ModelCallBlocked(
            make_budget_assessment(
                estimated_input_tokens=801,
                input_budget_tokens=750,
            )
        )
        conversation = Conversation()

        result = RunRuntime(
            model_calls,
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        ).run(conversation, "hello")

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(result.final_reason, "context_budget_exceeded")
        self.assertEqual(
            conversation.protocol_messages(),
            [{"role": "user", "content": "hello"}],
        )
        self.assertEqual(result.checkpoints[-1].data["overflow_tokens"], 51)

    def test_context_limit_error_blocks_without_retry(self):
        runner = RunRuntime(
            model_call_service(ContextLimitLLMClient()),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
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
        context_preparation = Mock(wraps=context_preparation_service())
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")

        result = RunRuntime(
            model_calls=model_call_service(llm_client),
            model="test-model",
            runtime_mode=mode,
            context_preparation=context_preparation,
            **runtime_tool_dependencies(),
        ).run(conversation, "hello")

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(context_preparation.prepare.call_count, 1)
        self.assertEqual(mode.instruction_calls, 1)
        self.assertIs(llm_client.messages[0], llm_client.messages[1])
        self.assertIn("第一轮指令", llm_client.messages[0][0]["content"])
        self.assertEqual(
            conversation.records[0].payload["content"],
            "system prompt",
        )

    def test_each_round_builds_a_snapshot_with_current_instructions(self):
        llm_client = RecordingLLMClient(
            [
                LLMResponse(type="text", content="draft"),
                LLMResponse(type="text", content="done"),
            ]
        )
        mode = ChangingInstructionsMode()
        context_preparation = Mock(wraps=context_preparation_service())
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")

        result = RunRuntime(
            model_calls=model_call_service(llm_client),
            model="test-model",
            runtime_mode=mode,
            context_preparation=context_preparation,
            **runtime_tool_dependencies(),
        ).run(conversation, "hello")

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(context_preparation.prepare.call_count, 2)
        self.assertEqual(mode.instruction_calls, 2)
        self.assertIn("第一轮指令", llm_client.messages[0][0]["content"])
        self.assertIn("第二轮指令", llm_client.messages[1][0]["content"])


if __name__ == "__main__":
    unittest.main()
