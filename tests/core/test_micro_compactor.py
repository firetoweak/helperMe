import json
import unittest

from core.context import ContextState, MicroCompactor
from core.context.composition import parse_tool_result_meta
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.tools_runtime.tools_protocol import validate_tool_message_chain


def tool_result(call_id: str, ok: bool = True, size: int = 100) -> dict[str, str]:
    return {
        "tool_call_id": call_id,
        "content": json.dumps(
            {
                "ok": ok,
                "code": "OK" if ok else "FAILED",
                "data": {"content": "x" * size},
                "error": None if ok else "tool failed",
                "hint": None,
            }
        ),
    }


class MicroCompactorTest(unittest.TestCase):
    def test_dehydrates_tool_body_but_keeps_protocol_shell(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("读取文件")
        conversation.add_assistant(
            LLMResponse(
                type="tool_calls",
                calls=[ToolCall("call-1", "read_file", '{"path":"a.txt"}')],
            )
        )
        conversation.add_tools_result([tool_result("call-1", size=500)])
        conversation.add_assistant(
            LLMResponse(type="text", content="已经读取并分析")
        )
        tool_record = conversation.records[3]
        original_payloads = [
            record.payload.copy() for record in conversation.records
        ]
        artifacts = {tool_record.message_id: "art_" + "a" * 32}

        projected = MicroCompactor().dehydrate(
            conversation.records,
            artifacts,
        )

        self.assertEqual(
            [message["role"] for message in projected],
            ["system", "user", "assistant", "tool", "assistant"],
        )
        self.assertIn("tool_calls", projected[2])
        self.assertEqual(
            projected[2]["tool_calls"][0]["function"]["name"],
            "read_file",
        )
        externalized, artifact_id = parse_tool_result_meta(
            projected[3]["content"]
        )
        self.assertTrue(externalized)
        self.assertEqual(artifact_id, artifacts[tool_record.message_id])
        self.assertLess(
            len(projected[3]["content"]),
            len(tool_record.payload["content"]),
        )
        self.assertTrue(validate_tool_message_chain(projected).ok)
        self.assertEqual(
            [record.payload for record in conversation.records],
            original_payloads,
        )

    def test_does_not_dehydrate_without_artifact_mapping(self):
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

        projected = MicroCompactor().dehydrate(conversation.records, {})

        self.assertEqual(projected, conversation.protocol_messages())

    def test_rejects_artifact_mapping_to_non_tool_message(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("hello")
        user_id = conversation.records[1].message_id

        with self.assertRaisesRegex(ValueError, "只能指向 tool"):
            MicroCompactor().dehydrate(
                conversation.records,
                {user_id: "art_" + "b" * 32},
            )


if __name__ == "__main__":
    unittest.main()
