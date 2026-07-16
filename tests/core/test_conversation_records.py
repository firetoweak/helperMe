import unittest

from core.context import ContextManager, ContextRequest
from core.messages import Conversation, ConversationMessage
from core.model_call import LLMResponse, ToolCall


class ConversationRecordTest(unittest.TestCase):
    def test_each_record_has_a_stable_unique_message_id(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("处理任务")
        conversation.add_assistant(
            LLMResponse(
                type="text",
                content="正在处理",
            )
        )

        records = conversation.records

        self.assertTrue(
            all(isinstance(record, ConversationMessage) for record in records)
        )
        self.assertTrue(all(record.message_id for record in records))
        self.assertEqual(
            len({record.message_id for record in records}),
            len(records),
        )

        existing_ids = [record.message_id for record in records]
        conversation.add_user("继续")
        self.assertEqual(
            [record.message_id for record in conversation.records[:-1]],
            existing_ids,
        )

    def test_message_id_is_separate_from_openai_payload(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_assistant(
            LLMResponse(
                type="tool_calls",
                calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments='{"path": "notes.txt"}',
                    )
                ],
            )
        )
        conversation.add_tools_result(
            [
                {
                    "tool_call_id": "call-1",
                    "content": '{"ok": true}',
                }
            ]
        )

        assistant_record, tool_record = conversation.records[-2:]

        self.assertNotIn("message_id", assistant_record.payload)
        self.assertNotIn("message_id", tool_record.payload)
        self.assertEqual(
            assistant_record.payload["tool_calls"][0]["id"],
            "call-1",
        )
        self.assertEqual(tool_record.payload["tool_call_id"], "call-1")
        self.assertNotEqual(
            assistant_record.message_id,
            tool_record.message_id,
        )

    def test_context_manager_projects_payload_without_internal_identity(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("处理任务")

        context = ContextManager().build(
            ContextRequest(
                conversation_records=conversation.records,
                runtime_instructions=["遵循当前计划"],
            )
        )

        self.assertEqual(
            [message["role"] for message in context.messages],
            ["system", "user"],
        )
        self.assertIn("遵循当前计划", context.messages[0]["content"])
        self.assertEqual(context.messages[1]["content"], "处理任务")
        self.assertTrue(
            all("message_id" not in message for message in context.messages)
        )

        context.messages[1]["content"] = "changed snapshot"
        self.assertEqual(
            conversation.records[1].payload["content"],
            "处理任务",
        )


if __name__ == "__main__":
    unittest.main()
