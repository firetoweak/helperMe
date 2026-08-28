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


class LLMClientUsageBoundaryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = object.__new__(LLMClient)

    async def test_reads_vllm_cached_prompt_tokens(self):
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None)
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=3,
                prompt_tokens_details=SimpleNamespace(cached_tokens=96),
            ),
        )

        async def create(*_args):
            return completion

        self.client.completions_create = create
        result = await self.client.chat([], "model")

        self.assertEqual(result.usage.cached_input_tokens, 96)
        self.assertEqual(result.usage.uncached_input_tokens, 24)

    async def test_missing_prompt_details_means_no_cached_tokens(self):
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None)
                )
            ],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=1),
        )

        async def create(*_args):
            return completion

        self.client.completions_create = create
        result = await self.client.chat([], "model")

        self.assertEqual(result.usage.cached_input_tokens, 0)

if __name__ == "__main__":
    unittest.main()
