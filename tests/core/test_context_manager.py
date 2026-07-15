import unittest

from core.context_manager import ContextManager, ContextRequest


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
                conversation_messages=source,
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
                conversation_messages=source,
                runtime_instructions=["follow the current plan"],
            )
        )

        self.assertIn("system prompt", context.messages[0]["content"])
        self.assertIn("follow the current plan", context.messages[0]["content"])
        self.assertEqual(source[0]["content"], "system prompt")

    def test_build_rejects_runtime_instructions_without_system_message(self):
        request = ContextRequest(
            conversation_messages=[{"role": "user", "content": "hello"}],
            runtime_instructions=["follow the current plan"],
        )

        with self.assertRaisesRegex(ValueError, "system"):
            self.manager.build(request)

    def test_build_rejects_invalid_tool_message_chain(self):
        request = ContextRequest(
            conversation_messages=[
                {"role": "system", "content": "system prompt"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "demo", "arguments": "{}"},
                        }
                    ],
                },
            ],
            runtime_instructions=[],
        )

        with self.assertRaises(ValueError):
            self.manager.build(request)


if __name__ == "__main__":
    unittest.main()
