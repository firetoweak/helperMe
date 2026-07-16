import unittest

from core.context import ContextManager, ContextRequest, ContextState
from core.messages import Conversation
from core.model_call import LLMResponse
from core.session_state import Session


class ContextStateTest(unittest.TestCase):
    def test_initial_state_has_no_summary_or_boundary(self):
        state = ContextState()

        self.assertIsNone(state.summary)
        self.assertIsNone(state.compacted_through_message_id)

    def test_summary_and_boundary_must_exist_together(self):
        invalid_states = (
            {"summary": "handoff", "compacted_through_message_id": None},
            {"summary": None, "compacted_through_message_id": "message-1"},
        )

        for values in invalid_states:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    ContextState(**values)

    def test_session_owns_an_initial_context_state(self):
        session = Session(id="session-1")

        self.assertEqual(session.context_state, ContextState())


class ContextStateProjectionTest(unittest.TestCase):
    def test_summary_replaces_compacted_prefix_and_preserves_suffix(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("旧任务")
        conversation.add_assistant(
            LLMResponse(type="text", content="旧进展")
        )
        conversation.add_user("继续处理")
        boundary = conversation.records[2].message_id
        state = ContextState(
            summary="已经完成前期分析。",
            compacted_through_message_id=boundary,
        )

        context = ContextManager().build(
            ContextRequest(
                conversation_records=conversation.records,
                runtime_instructions=[],
                context_state=state,
            )
        )

        self.assertEqual(
            [message["role"] for message in context.messages],
            ["system", "assistant", "user"],
        )
        self.assertEqual(context.messages[0]["content"], "system prompt")
        self.assertIn("工作交接摘要", context.messages[1]["content"])
        self.assertIn(state.summary, context.messages[1]["content"])
        self.assertEqual(context.messages[2]["content"], "继续处理")

    def test_projection_rejects_unknown_compaction_boundary(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("处理任务")
        state = ContextState(
            summary="handoff",
            compacted_through_message_id="missing-message",
        )

        with self.assertRaisesRegex(ValueError, "missing-message"):
            ContextManager().build(
                ContextRequest(
                    conversation_records=conversation.records,
                    runtime_instructions=[],
                    context_state=state,
                )
            )

    def test_projection_rejects_system_message_as_compaction_boundary(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("处理任务")
        state = ContextState(
            summary="handoff",
            compacted_through_message_id=conversation.records[0].message_id,
        )

        with self.assertRaisesRegex(ValueError, "system"):
            ContextManager().build(
                ContextRequest(
                    conversation_records=conversation.records,
                    runtime_instructions=[],
                    context_state=state,
                )
            )


if __name__ == "__main__":
    unittest.main()
