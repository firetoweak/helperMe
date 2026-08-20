import unittest

from core.context import ContextManager, ContextRequest
from core.messages import ConversationMessage


def records(*payloads):
    return [
        ConversationMessage(message_id=f"message-{index}", payload=payload)
        for index, payload in enumerate(payloads, start=1)
    ]


class ContextManagerTest(unittest.TestCase):
    def setUp(self):
        self.manager = ContextManager()

    def test_build_copies_conversation_messages_without_mutating_source(self):
        source = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ]

        context = self.manager.build(
            ContextRequest(
                conversation_records=records(*source),
                runtime_instructions=[],
            )
        )

        self.assertEqual(context.messages, source)
        self.assertIsNot(context.messages, source)
        self.assertIsNot(context.messages[0], source[0])

        context.messages[0]["content"] = "changed snapshot"
        self.assertEqual(source[0]["content"], "system prompt")

    def test_build_injects_runtime_instructions_into_system_snapshot(self):
        source = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "hello"},
        ]

        context = self.manager.build(
            ContextRequest(
                conversation_records=records(*source),
                runtime_instructions=["follow the current plan"],
            )
        )

        self.assertIn("system prompt", context.messages[0]["content"])
        self.assertIn("follow the current plan", context.messages[0]["content"])
        self.assertEqual(source[0]["content"], "system prompt")

    def test_contextual_fragment_is_user_role_projection_only(self):
        source = records(
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "完成任务"},
        )

        context = self.manager.build(ContextRequest(
            conversation_records=source,
            runtime_instructions=[],
            contextual_user_fragments=[
                "<environment_context><cwd>/repo</cwd></environment_context>"
            ],
        ))

        self.assertEqual(context.messages[1]["role"], "user")
        self.assertIn("<environment_context>", context.messages[1]["content"])
        self.assertEqual(source[1].payload["content"], "完成任务")
        self.assertEqual(len(source), 2)

if __name__ == "__main__":
    unittest.main()
