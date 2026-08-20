import unittest
from unittest.mock import AsyncMock, patch

from core.messages import Conversation
from core.model_call import InvalidLLMResponse, LLMResponse, ToolCall
from core.model_call.client import LLMTransientError
from core.runtime_modes import PlainMode
from core.tools_runtime.turn_invocation import TurnInvocation
from core.tools_runtime.turn_runtime import (
    TurnControl,
    TurnRuntime as CoreTurnRuntime,
    TurnStatus,
)
from tests.core.environment_test_support import BoundTurnRuntime as TurnRuntime
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


class TurnEnvironmentContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_internal_environment_binding_is_not_wrapped(self):
        runner = CoreTurnRuntime(
            model_call_service(RecordingLLMClient([])),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        with self.assertRaises(AttributeError):
            await runner.run(
                Conversation(),
                "hello",
                invocation=TurnInvocation(),
            )


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, model, tools=None):
        return call_result(self.responses.pop(0))


class RecordingProgressSink:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def emit(self, text: str) -> None:
        self.events.append(f"progress:{text}")


class InterruptingLLMClient:
    def __init__(self, control, responses):
        self.control = control
        self.responses = list(responses)
        self.calls = 0

    async def chat(self, messages, model, tools=None):
        self.calls += 1
        if self.calls == 1:
            self.control.request_interrupt("测试中断")
        return call_result(self.responses.pop(0))


class EmptyResponseLLMClient:
    def __init__(self):
        self.call_count = 0

    async def chat(self, messages, model, tools=None):
        self.call_count += 1
        raise InvalidLLMResponse(
            "empty_model_response",
            "model returned empty response",
        )


class TurnRuntimeStopGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_turn_evidence_preserves_raw_result_before_externalization(self):
        raw_result = {
            "ok": True,
            "code": "LARGE_RESULT",
            "data": {"payload": "x" * 20_000},
            "error": None,
            "hint": None,
        }
        llm = RecordingLLMClient(
            [
                LLMResponse(calls=(ToolCall("call-1", "demo", "{}"),)),
                LLMResponse(content="完成"),
            ]
        )
        conversation = Conversation()

        result = await TurnRuntime(
            model_call_service(llm),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(raw_result),
        ).run(conversation, "执行工具")

        self.assertEqual(
            result.evidence.steps[0].result["data"]["payload"],
            "x" * 20_000,
        )
        tool_message = conversation.protocol_messages()[2]
        self.assertIn("externalized", tool_message["content"])

    async def test_mixed_response_is_saved_and_emitted_before_tool_execution(self):
        events: list[str] = []
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    content="我先检查项目结构。",
                    calls=(ToolCall("call-1", "demo", "{}"),),
                ),
                LLMResponse(content="检查完成"),
            ]
        )
        dependencies = runtime_tool_dependencies(SUCCESS)
        async def execute(_name, _arguments):
            events.append("tool:demo")
            return SUCCESS

        dependencies["tools_executor"].execute = execute
        conversation = Conversation()

        result = await TurnRuntime(
            model_call_service(llm),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            progress_sink=RecordingProgressSink(events),
            **dependencies,
        ).run(conversation, "检查项目")

        self.assertEqual(result.answer, "检查完成")
        self.assertEqual(
            events,
            ["progress:我先检查项目结构。", "tool:demo"],
        )
        assistant = conversation.protocol_messages()[1]
        self.assertEqual(assistant["content"], "我先检查项目结构。")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call-1")

    async def test_unverified_write_cannot_complete(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(
                    calls=(ToolCall("write-1", "write_file", "{}"),),
                ),
                LLMResponse(content="尚未验证的回答"),
                LLMResponse(
                    calls=(ToolCall("verify-1", "get_changes", "{}"),),
                ),
                LLMResponse(content="最终回答"),
            ]
        )
        conversation = Conversation()

        result = await TurnRuntime(
            model_call_service(llm),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(SUCCESS),
        ).run(
            conversation,
            "修改文件",
            max_steps=4,
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


class TurnRuntimeInterruptTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupts_after_complete_tool_batch(self):
        control = TurnControl()
        llm = InterruptingLLMClient(
            control,
            [
                LLMResponse(
                    calls=(ToolCall("call-1", "demo", "{}"),),
                )
            ],
        )
        conversation = Conversation()

        result = await TurnRuntime(
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
        self.assertEqual(result.final_reason, "turn_interrupted")
        self.assertTrue(
            validate_tool_message_chain(conversation.protocol_messages()).ok
        )

    async def test_interrupt_waits_for_verification(self):
        control = TurnControl()
        llm = InterruptingLLMClient(
            control,
            [
                LLMResponse(
                    calls=(ToolCall("write-1", "write_file", "{}"),),
                ),
                LLMResponse(
                    calls=(ToolCall("verify-1", "get_changes", "{}"),),
                ),
            ],
        )
        conversation = Conversation()

        result = await TurnRuntime(
            model_call_service(llm),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(SUCCESS),
        ).run(
            conversation,
            "修改文件",
            max_steps=2,
            control=control,
        )

        self.assertEqual(result.status, "interrupted")
        self.assertEqual(
            [checkpoint.reason for checkpoint in result.checkpoints][-2:],
            ["tool_batch_completed", "turn_interrupted"],
        )
        self.assertIn(
            "verification_required",
            [checkpoint.reason for checkpoint in result.checkpoints],
        )
        self.assertTrue(
            validate_tool_message_chain(conversation.protocol_messages()).ok
        )


class TurnRuntimeInvalidLLMResponseTest(unittest.IsolatedAsyncioTestCase):
    @patch("core.tools_runtime.turn_runtime.asyncio.sleep", new_callable=AsyncMock)
    async def test_empty_response_retries_then_fails_without_conversation_pollution(
        self,
        _sleep,
    ):
        llm_client = EmptyResponseLLMClient()
        runner = TurnRuntime(
            model_call_service(llm_client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )
        conversation = Conversation()

        result = await runner.run(conversation, "hello")

        self.assertEqual(result.status, TurnStatus.FAILED)
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

    @patch("core.tools_runtime.turn_runtime.asyncio.sleep", new_callable=AsyncMock)
    async def test_empty_response_retry_can_recover(self, _sleep):
        class RecoveringLLMClient:
            def __init__(self):
                self.call_count = 0

            async def chat(self, messages, model, tools=None):
                self.call_count += 1
                if self.call_count == 1:
                    raise InvalidLLMResponse(
                        "empty_model_response",
                        "model returned empty response",
                    )
                return call_result(
                    LLMResponse(content="done")
                )

        llm_client = RecoveringLLMClient()
        runner = TurnRuntime(
            model_call_service(llm_client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = await runner.run(Conversation(), "hello")

        self.assertEqual(result.status, TurnStatus.COMPLETED)
        self.assertEqual(llm_client.call_count, 2)

    async def test_internal_llm_client_bug_is_not_retried_or_converted(self):
        class BrokenLLMClient:
            def __init__(self):
                self.call_count = 0

            async def chat(self, messages, model, tools=None):
                self.call_count += 1
                raise RuntimeError("client bug")

        llm_client = BrokenLLMClient()
        runner = TurnRuntime(
            model_call_service(llm_client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        with self.assertRaisesRegex(RuntimeError, "client bug"):
            await runner.run(Conversation(), "hello")

        self.assertEqual(llm_client.call_count, 1)

    @patch("core.tools_runtime.turn_runtime.asyncio.sleep", new_callable=AsyncMock)
    async def test_explicit_transient_llm_error_is_retried(self, _sleep):
        class TransientLLMClient:
            def __init__(self):
                self.call_count = 0

            async def chat(self, messages, model, tools=None):
                self.call_count += 1
                if self.call_count == 1:
                    raise LLMTransientError("temporary unavailable")
                return call_result(LLMResponse(content="done"))

        llm_client = TransientLLMClient()
        runner = TurnRuntime(
            model_call_service(llm_client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

        result = await runner.run(Conversation(), "hello")

        self.assertEqual(result.status, TurnStatus.COMPLETED)
        self.assertEqual(llm_client.call_count, 2)
        self.assertTrue(any(
            checkpoint.reason == "llm_retry"
            for checkpoint in result.checkpoints
        ))


if __name__ == "__main__":
    unittest.main()
