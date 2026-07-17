import json
import unittest

from core.context import ContextManager, ContextRequest, ContextState
from core.context.composition import parse_tool_result_meta
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.session_state import Session


class ContextStateTest(unittest.TestCase):
    def test_initial_state_has_no_summary_or_artifacts(self):
        state = ContextState()

        self.assertIsNone(state.summary)
        self.assertIsNone(state.summarized_through_message_id)
        self.assertEqual(state.tool_artifacts, {})

    def test_summary_and_boundary_must_exist_together(self):
        invalid_states = (
            {"summary": "handoff", "summarized_through_message_id": None},
            {"summary": None, "summarized_through_message_id": "message-1"},
        )

        for values in invalid_states:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    ContextState(**values)

    def test_tool_artifacts_can_exist_without_summary(self):
        state = ContextState(
            tool_artifacts={"message-3": "art_" + "d" * 32}
        )

        self.assertIsNone(state.summary)
        self.assertIsNone(state.summarized_through_message_id)
        self.assertEqual(
            state.tool_artifacts["message-3"],
            "art_" + "d" * 32,
        )

    def test_session_owns_an_initial_context_state(self):
        session = Session(id="session-1")

        self.assertEqual(session.context_state, ContextState())


class ContextStateProjectionTest(unittest.TestCase):
    @staticmethod
    def _add_successful_consumed_tool_batch(conversation):
        conversation.add_assistant(
            LLMResponse(
                type="tool_calls",
                calls=[ToolCall("call-1", "read_file", '{"path":"a.txt"}')],
            )
        )
        conversation.add_tools_result(
            [
                {
                    "tool_call_id": "call-1",
                    "content": json.dumps(
                        {
                            "ok": True,
                            "code": "OK",
                            "data": {"content": "x" * 100},
                            "error": None,
                            "hint": None,
                        }
                    ),
                }
            ]
        )
        conversation.add_assistant(
            LLMResponse(type="text", content="已消费工具结果")
        )

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
            summarized_through_message_id=boundary,
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
            summarized_through_message_id="missing-message",
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
            summarized_through_message_id=conversation.records[0].message_id,
        )

        with self.assertRaisesRegex(ValueError, "system"):
            ContextManager().build(
                ContextRequest(
                    conversation_records=conversation.records,
                    runtime_instructions=[],
                    context_state=state,
                )
            )

    def test_tool_artifacts_are_applied_to_model_context(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("读取文件")
        self._add_successful_consumed_tool_batch(conversation)
        conversation.add_user("继续")
        tool_record = next(
            record
            for record in conversation.records
            if record.payload.get("role") == "tool"
        )
        artifact_id = "art_" + "e" * 32
        state = ContextState(
            tool_artifacts={tool_record.message_id: artifact_id}
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
            ["system", "user", "assistant", "tool", "assistant", "user"],
        )
        self.assertIn("tool_calls", context.messages[2])
        externalized, resolved = parse_tool_result_meta(
            context.messages[3]["content"]
        )
        self.assertTrue(externalized)
        self.assertEqual(resolved, artifact_id)

    def test_summary_is_applied_before_tool_dehydration(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("旧任务")
        conversation.add_assistant(
            LLMResponse(type="text", content="旧进展")
        )
        summary_boundary = conversation.records[-1].message_id
        conversation.add_user("读取文件")
        self._add_successful_consumed_tool_batch(conversation)
        conversation.add_user("继续")
        tool_record = next(
            record
            for record in conversation.records
            if record.payload.get("role") == "tool"
        )
        artifact_id = "art_" + "f" * 32
        state = ContextState(
            summary="旧任务已完成。",
            summarized_through_message_id=summary_boundary,
            tool_artifacts={tool_record.message_id: artifact_id},
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
            ["system", "assistant", "user", "assistant", "tool", "assistant", "user"],
        )
        self.assertIn("工作交接摘要", context.messages[1]["content"])
        self.assertIn("tool_calls", context.messages[3])
        _, resolved = parse_tool_result_meta(context.messages[4]["content"])
        self.assertEqual(resolved, artifact_id)


if __name__ == "__main__":
    unittest.main()
