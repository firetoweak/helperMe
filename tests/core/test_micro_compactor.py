import json
import unittest

from core.context import MicroCompactor
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.tools_runtime.tools_protocol import validate_tool_message_chain


def tool_result(call_id: str, ok: bool = True) -> dict[str, str]:
    return {
        "tool_call_id": call_id,
        "content": json.dumps(
            {
                "ok": ok,
                "code": "OK" if ok else "FAILED",
                "data": {"content": "x" * 100},
                "error": None if ok else "tool failed",
                "hint": None,
            }
        ),
    }


class MicroCompactorTest(unittest.TestCase):
    def test_compacts_a_successful_consumed_tool_batch_atomically(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("读取文件")
        conversation.add_assistant(
            LLMResponse(
                type="tool_calls",
                calls=[ToolCall("call-1", "read_file", '{"path":"a.txt"}')],
            )
        )
        conversation.add_tools_result([tool_result("call-1")])
        conversation.add_assistant(
            LLMResponse(type="text", content="已经读取并分析")
        )
        conversation.add_user("继续")
        boundary = conversation.records[4].message_id
        original_payloads = [
            record.payload.copy() for record in conversation.records
        ]

        projected = MicroCompactor().compact(
            conversation.records,
            through_message_id=boundary,
        )

        self.assertEqual(
            [message["role"] for message in projected],
            ["system", "user", "assistant", "assistant", "user"],
        )
        self.assertNotIn("tool_calls", projected[2])
        self.assertIn("工具批次已压缩", projected[2]["content"])
        self.assertEqual(projected[3]["content"], "已经读取并分析")
        self.assertTrue(validate_tool_message_chain(projected).ok)
        self.assertEqual(
            [record.payload for record in conversation.records],
            original_payloads,
        )

    def test_compacts_a_multi_call_batch_only_when_every_result_succeeds(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("读取文件")
        conversation.add_assistant(
            LLMResponse(
                type="tool_calls",
                calls=[
                    ToolCall("call-1", "read_file", '{"path":"a.txt"}'),
                    ToolCall("call-2", "read_file", '{"path":"b.txt"}'),
                ],
            )
        )
        conversation.add_tools_result(
            [tool_result("call-1"), tool_result("call-2", ok=False)]
        )
        conversation.add_assistant(
            LLMResponse(type="text", content="根据失败结果调整")
        )
        boundary = conversation.records[-1].message_id

        projected = MicroCompactor().compact(
            conversation.records,
            through_message_id=boundary,
        )

        self.assertIn("tool_calls", projected[2])
        self.assertEqual(
            [message["role"] for message in projected[2:5]],
            ["assistant", "tool", "tool"],
        )

    def test_does_not_compact_a_batch_before_a_later_assistant_consumes_it(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("读取文件")
        conversation.add_assistant(
            LLMResponse(
                type="tool_calls",
                calls=[ToolCall("call-1", "read_file", '{"path":"a.txt"}')],
            )
        )
        conversation.add_tools_result([tool_result("call-1")])
        boundary = conversation.records[-1].message_id

        projected = MicroCompactor().compact(
            conversation.records,
            through_message_id=boundary,
        )

        self.assertEqual(
            projected,
            conversation.protocol_messages(),
        )

    def test_rejects_an_unknown_boundary(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")

        with self.assertRaisesRegex(ValueError, "missing-message"):
            MicroCompactor().compact(
                conversation.records,
                through_message_id="missing-message",
            )


if __name__ == "__main__":
    unittest.main()
