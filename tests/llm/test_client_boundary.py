from types import SimpleNamespace
import unittest

from helperme.llm.client import LLMClient
from helperme.llm.types import InvalidLLMResponse


class LLMClientBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = object.__new__(LLMClient)

    def test_rejects_non_array_tool_calls_instead_of_treating_as_empty(self):
        response = SimpleNamespace(content="done", tool_calls={})

        with self.assertRaisesRegex(
            InvalidLLMResponse,
            "array|null",
        ):
            self.client._parse_response(response)

    def test_rejects_missing_tool_call_fields(self):
        response = SimpleNamespace(
            content="",
            tool_calls=[SimpleNamespace(id="call-1")],
        )

        with self.assertRaisesRegex(
            InvalidLLMResponse,
            "tool call fields",
        ):
            self.client._parse_response(response)


if __name__ == "__main__":
    unittest.main()
