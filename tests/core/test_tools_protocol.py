import json
import unittest

from core.tools_runtime.tools_protocol import (
    build_tool_messages,
    validate_tool_message_chain,
)
from core.tools_runtime.tools_state import ToolsState


class ToolsProtocolTest(unittest.TestCase):
    def test_complete_tool_chain_is_valid(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1"}],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "{}",
            },
        ]

        self.assertTrue(validate_tool_message_chain(messages).ok)

    def test_dangling_tool_call_is_invalid(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1"}],
            }
        ]

        result = validate_tool_message_chain(messages)

        self.assertFalse(result.ok)
        self.assertEqual(result.pending_tool_call_ids, ["call-1"])

    def test_build_tool_messages_only_exports_completed_steps(self):
        state = ToolsState()
        completed = state.add_call("call-1", "demo", "{}")
        state.add_call("call-2", "demo", "{}")
        state.add_result(
            "call-1",
            {"ok": True, "code": "OK", "data": None, "error": None},
        )

        messages = build_tool_messages([completed, state.get_step("call-2")], json.dumps)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["tool_call_id"], "call-1")


if __name__ == "__main__":
    unittest.main()
