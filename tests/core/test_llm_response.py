import unittest
from types import SimpleNamespace

from core.llm_client import LLMClient
from core.messages import InvalidLLMResponse, LLMResponse, ToolCall


class LLMResponseContractTest(unittest.TestCase):
    def test_text_response_requires_non_empty_content(self):
        for content in ("", "   ", None):
            with self.subTest(content=content):
                with self.assertRaises(InvalidLLMResponse) as raised:
                    LLMResponse(type="text", content=content)

                self.assertEqual(raised.exception.code, "empty_model_response")

    def test_tool_calls_response_requires_non_empty_calls(self):
        for calls in (None, []):
            with self.subTest(calls=calls):
                with self.assertRaises(InvalidLLMResponse):
                    LLMResponse(type="tool_calls", calls=calls)

    def test_valid_response_variants(self):
        text = LLMResponse(type="text", content="done")
        tool_calls = LLMResponse(
            type="tool_calls",
            calls=[ToolCall(id="call-1", name="read_file", arguments="{}")],
        )

        self.assertEqual(text.content, "done")
        self.assertEqual(tool_calls.calls[0].id, "call-1")

    def test_client_parser_rejects_empty_sdk_response(self):
        response = SimpleNamespace(tool_calls=None, content=None)

        with self.assertRaises(InvalidLLMResponse) as raised:
            LLMClient._parse_response(None, response)

        self.assertEqual(raised.exception.code, "empty_model_response")


if __name__ == "__main__":
    unittest.main()
