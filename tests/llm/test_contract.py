import unittest

from helperme.llm.types import InvalidLLMResponse, LLMResponse, ToolCall


class LLMResponseContractTest(unittest.TestCase):
    def test_text_response_requires_non_empty_content(self):
        for content in ("", "   "):
            with self.subTest(content=content):
                with self.assertRaises(InvalidLLMResponse):
                    LLMResponse(content=content)

    def test_tool_calls_may_have_empty_content(self):
        response = LLMResponse(
            content="",
            calls=(ToolCall("c1", "read_file", "{}"),),
        )
        self.assertEqual(response.calls[0].name, "read_file")
