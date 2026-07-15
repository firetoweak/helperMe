import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from core.model_call import InvalidLLMResponse, LLMResponse, ToolCall
from core.model_call.client import (
    LLMClient,
)


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

    def test_client_returns_response_with_real_usage(self):
        client = object.__new__(LLMClient)
        client.completions_create = Mock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            tool_calls=None,
                            content="done",
                        )
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=120,
                    completion_tokens=30,
                ),
            )
        )

        result = client.chat([], "test-model", tools=None)

        self.assertEqual(result.response.content, "done")
        self.assertEqual(result.usage.input_tokens, 120)
        self.assertEqual(result.usage.output_tokens, 30)
        self.assertEqual(result.usage.total_tokens, 150)


if __name__ == "__main__":
    unittest.main()
