from __future__ import annotations

import unittest

from core.context import (
    ContextBudget,
    ContextManager,
    ContextPreparationService,
    MicroCompactionConfig,
    MicroCompactionPolicy,
    ModelBudgetConfig,
    SummaryGeneration,
)
from core.model_call import LLMResponse, ToolCall
from core.model_call.service import ModelCallService
from core.runtime_artifacts import ToolResultExternalizer, ToolResultLimit
from core.runtime_modes import PlainMode
from core.session import SessionRuntime
from core.tool_registry import EmptyInput, PydanticParameters, ToolRegistry, ToolSpec
from core.tools_runtime.turn_runtime import TurnRuntime, TurnStatus
from core.tools_runtime.tools_executor import ToolsExecutor
from tests.core.llm_test_support import (
    CharacterEstimator,
    MemoryArtifactStore,
    call_result,
)


class RecordingSummaryGenerator:
    def __init__(self, summaries: list[str]) -> None:
        self._summaries = list(summaries)
        self.contexts = []

    async def generate(self, model_context):
        self.contexts.append(model_context)
        return SummaryGeneration(
            summary=self._summaries.pop(0),
            input_tokens=100,
            output_tokens=10,
        )


class ScriptedLLMClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.messages = []
        self.before_response = None

    async def chat(self, messages, model, tools=None):
        self.messages.append(messages)
        if self.before_response is not None:
            callback = self.before_response
            self.before_response = None
            callback()
        return call_result(self._responses.pop(0))


def make_runtime(
    *,
    llm_client: ScriptedLLMClient,
    summary_generator: RecordingSummaryGenerator,
    context_limit: int,
    registry: ToolRegistry | None = None,
) -> TurnRuntime:
    manager = ContextManager()
    preparation_budget = ContextBudget(
        CharacterEstimator(),
        ModelBudgetConfig(context_limit=context_limit, input_ratio=0.9),
    )
    artifact_store = MemoryArtifactStore()
    preparation = ContextPreparationService(
        context_manager=manager,
        micro_compaction_policy=MicroCompactionPolicy(
            context_manager=manager,
            context_budget=preparation_budget,
            config=MicroCompactionConfig(recent_protection_tokens=100),
            artifact_store=artifact_store,
        ),
        context_budget=preparation_budget,
        summary_generator=summary_generator,
    )
    model_calls = ModelCallService(
        llm_client=llm_client,
        context_budget=ContextBudget(
            CharacterEstimator(),
            ModelBudgetConfig(context_limit=100_000, input_ratio=0.9),
        ),
    )
    return TurnRuntime(
        model_calls=model_calls,
        model="test-model",
        runtime_mode=PlainMode(),
        context_preparation=preparation,
        tools_executor=ToolsExecutor(registry or ToolRegistry()),
        tool_result_externalizer=ToolResultExternalizer(
            artifact_store,
            ToolResultLimit(),
        ),
    )


class SafeCompressionEndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_session_incrementally_summarizes_s1_plus_delta_into_s2(self):
        summary_generator = RecordingSummaryGenerator(["S1", "S2"])
        llm_client = ScriptedLLMClient([
            LLMResponse(content="DELTA_ONE " * 60),
            LLMResponse(content="second turn done"),
        ])
        session_runtime = SessionRuntime(
            turn_runtime=make_runtime(
                llm_client=llm_client,
                summary_generator=summary_generator,
                context_limit=300,
            )
        )
        session = session_runtime.create_session("session-1", "system")
        session.conversation.add_user("OLD_ORIGINAL " * 60)
        session.conversation.add_assistant(
            LLMResponse(content="OLD_ANSWER " * 40)
        )

        first = await session_runtime.start("session-1", "turn-1", "first goal")
        first_boundary = session.context_state.summarized_through_message_id
        second = await session_runtime.start("session-1", "turn-2", "second goal")

        self.assertEqual(first.result.status, TurnStatus.COMPLETED)
        self.assertEqual(first.result.context_state.summary, "S1")
        self.assertEqual(second.result.status, TurnStatus.COMPLETED)
        self.assertEqual(session.context_state.summary, "S2")
        self.assertNotEqual(
            session.context_state.summarized_through_message_id,
            first_boundary,
        )
        self.assertEqual(len(summary_generator.contexts), 2)

        first_source = str(summary_generator.contexts[0].messages)
        second_source_messages = summary_generator.contexts[1].messages
        second_source = str(second_source_messages)
        self.assertIn("OLD_ORIGINAL", first_source)
        self.assertIn("OLD_ANSWER", first_source)
        self.assertTrue(any(
            message.get("content") == "工作交接摘要：\nS1"
            for message in second_source_messages
        ))
        self.assertIn("DELTA_ONE", second_source)
        self.assertNotIn("OLD_ORIGINAL", second_source)
        self.assertNotIn("OLD_ANSWER", second_source)

        self.assertTrue(any(
            message.get("content") == "工作交接摘要：\nS1"
            for message in llm_client.messages[0]
        ))
        self.assertTrue(any(
            message.get("content") == "工作交接摘要：\nS2"
            for message in llm_client.messages[1]
        ))
        conversation_text = str(session.conversation.protocol_messages())
        self.assertIn("OLD_ORIGINAL", conversation_text)
        self.assertIn("OLD_ANSWER", conversation_text)

    async def test_interrupt_resume_reuses_committed_summary_state(self):
        registry = ToolRegistry()
        async def ping(_raw):
            return {
                "ok": True,
                "code": "PONG",
                "data": {"value": "pong"},
            }

        registry.register(ToolSpec(
            name="ping",
            description="返回 pong。",
            parameters=PydanticParameters(EmptyInput),
            handler=ping,
        ))
        summary_generator = RecordingSummaryGenerator(["S1"])
        llm_client = ScriptedLLMClient([
            LLMResponse(
                calls=(ToolCall("call-1", "ping", "{}"),),
            ),
            LLMResponse(content="resumed done"),
        ])
        session_runtime = SessionRuntime(
            turn_runtime=make_runtime(
                llm_client=llm_client,
                summary_generator=summary_generator,
                context_limit=1_000,
                registry=registry,
            )
        )
        session = session_runtime.create_session("session-1", "system")
        session.conversation.add_user("OLD_ORIGINAL " * 120)
        session.conversation.add_assistant(
            LLMResponse(content="OLD_ANSWER " * 80)
        )
        llm_client.before_response = lambda: session_runtime.request_interrupt(
            "session-1",
            "benchmark_interrupt",
        )

        interrupted = await session_runtime.start(
            "session-1",
            "turn-1",
            "start work",
        )
        interrupted_state = session.context_state
        resumed = await session_runtime.resume(
            "session-1",
            "turn-2",
            "continue work",
        )

        self.assertEqual(interrupted.result.status, TurnStatus.INTERRUPTED)
        self.assertEqual(interrupted_state.summary, "S1")
        self.assertEqual(resumed.result.status, TurnStatus.COMPLETED)
        self.assertEqual(session.context_state.summary, "S1")
        self.assertEqual(
            session.context_state.summarized_through_message_id,
            interrupted_state.summarized_through_message_id,
        )
        self.assertEqual(len(summary_generator.contexts), 1)

        resumed_messages = llm_client.messages[1]
        resumed_context = str(resumed_messages)
        self.assertTrue(any(
            message.get("content") == "工作交接摘要：\nS1"
            for message in resumed_messages
        ))
        self.assertIn("continue work", resumed_context)
        self.assertIn("pong", resumed_context)
        self.assertNotIn("OLD_ORIGINAL", resumed_context)
        conversation_text = str(session.conversation.protocol_messages())
        self.assertIn("OLD_ORIGINAL", conversation_text)
        self.assertIn("OLD_ANSWER", conversation_text)


if __name__ == "__main__":
    unittest.main()
