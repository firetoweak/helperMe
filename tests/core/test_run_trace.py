import unittest
from unittest.mock import Mock

from core.context import ContextState, make_budget_assessment
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.session import SessionRuntime
from core.session.state import SessionEvent
from core.tools_runtime.run_runtime import RunRuntime, RunStatus
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    runtime_tool_dependencies,
)


class RunTraceTest(unittest.TestCase):
    def test_agent_round_emits_context_prepared_with_role_breakdown(self):
        model_calls = Mock()
        model_calls.call.return_value = call_result(
            LLMResponse(type="text", content="done")
        )
        conversation = Conversation()
        conversation.set_system_prompt("system")

        result = RunRuntime(
            model_calls,
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        ).run(conversation, "hello")

        prepared = [
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "context_prepared"
        ]
        self.assertEqual(len(prepared), 1)
        data = prepared[0].data
        self.assertEqual(data["stage"], "agent_round")
        self.assertEqual(data["round_index"], 1)
        composition = data["composition"]
        self.assertIn("by_role_tokens", composition)
        self.assertIn("tool", composition["by_role_tokens"])
        self.assertIn("tools_schema_tokens", composition)
        self.assertIn("tool_results", composition)
        self.assertIn("dehydrated_tool_tokens_estimate", composition)
        self.assertIn("dehydrated_tool_savings_estimate", composition)
        micro = data["micro_compaction"]
        self.assertIn("changed", micro)
        self.assertFalse(micro["changed"])
        self.assertIn("before_composition", micro)
        self.assertIn("after_composition", micro)
        self.assertIn("tool_window", micro)
        self.assertIn("recent_tool_chars", micro["tool_window"])
        self.assertIn("compressible_tool_chars", micro["tool_window"])

    def test_tool_batch_records_externalize_stats_from_outcome(self):
        huge = {
            "ok": True,
            "code": "OK",
            "data": {"content": "x" * 20_000},
            "error": None,
            "hint": None,
        }
        model_calls = Mock()
        model_calls.call.side_effect = [
            call_result(
                LLMResponse(
                    type="tool_calls",
                    calls=[
                        ToolCall(
                            id="call_1",
                            name="read_file",
                            arguments='{"path":"a.py"}',
                        )
                    ],
                )
            ),
            call_result(LLMResponse(type="text", content="done")),
        ]
        deps = runtime_tool_dependencies(execute_result=huge)
        conversation = Conversation()
        conversation.set_system_prompt("system")

        result = RunRuntime(
            model_calls,
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **deps,
        ).run(conversation, "read it")

        batches = [
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "tool_batch_completed"
        ]
        self.assertEqual(len(batches), 1)
        data = batches[0].data
        self.assertEqual(data["externalized_count"], 1)
        self.assertGreater(data["result_chars_before"], data["result_chars_after"])
        self.assertEqual(data["batch_size"], 1)
        self.assertGreater(data["result_chars_before"], 16_000)

    def test_level2_checkpoint_includes_before_after_composition(self):
        before = make_budget_assessment(900, 750)
        after = make_budget_assessment(500, 750)
        from core.context import (
            MicroCompactionDecision,
            ModelContext,
            PreparedContext,
            SummaryCompaction,
            SummaryGeneration,
            empty_tool_window_stats,
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
            context_state=ContextState(
                summary="handoff",
                summarized_through_message_id="old-message",
            ),
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

        compressed = next(
            checkpoint
            for checkpoint in result.checkpoints
            if checkpoint.reason == "level2_context_compressed"
        )
        self.assertEqual(
            compressed.data["before_composition"]["estimated_total_tokens"],
            900,
        )
        self.assertEqual(
            compressed.data["after_composition"]["estimated_total_tokens"],
            500,
        )

    def test_session_events_do_not_carry_composition(self):
        model_calls = Mock()
        model_calls.call.return_value = call_result(
            LLMResponse(type="text", content="done")
        )
        session_runtime = SessionRuntime(
            RunRuntime(
                model_calls,
                "test-model",
                PlainMode(),
                context_preparation_service(),
                **runtime_tool_dependencies(),
            )
        )
        session_runtime.create_session("s1", "system")
        outcome = session_runtime.start("s1", "r1", "hello")

        self.assertEqual(outcome.result.status, RunStatus.COMPLETED)
        for event in session_runtime.sessions["s1"].events:
            self.assertIsInstance(event, SessionEvent)
            self.assertFalse(hasattr(event, "composition"))
            self.assertEqual(
                set(event.__dataclass_fields__),
                {"kind", "session_id", "reason", "run_id", "occurred_at"},
            )
        prepared = [
            checkpoint
            for checkpoint in outcome.result.checkpoints
            if checkpoint.reason == "context_prepared"
        ]
        self.assertEqual(len(prepared), 1)


if __name__ == "__main__":
    unittest.main()
