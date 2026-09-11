from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import litellm

from helperme.llm.adapter import LiteLLMAdapter
from helperme.llm.api import LLMAuthenticationError
from helperme.llm.config import ModelConfig
from helperme.llm.types import InvalidLLMResponse


class _Message:
    def __init__(self, data: dict[str, object]):
        self.data = data

    def model_dump(self, *, exclude_none: bool):
        return {
            key: value
            for key, value in self.data.items()
            if not exclude_none or value is not None
        }


class LiteLLMAdapterBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = object.__new__(LiteLLMAdapter)

    def test_keeps_unknown_assistant_message_fields(self):
        response = _Message({
            "role": "assistant",
            "content": "done",
            "tool_calls": None,
            "reasoning_content": "",
            "provider_specific_fields": {"signature": "abc"},
        })

        parsed = self.adapter._parse_response(response)

        self.assertEqual(parsed.content, "done")
        self.assertEqual(parsed.message_extensions, {
            "reasoning_content": "",
            "provider_specific_fields": {"signature": "abc"},
        })

    def test_rejects_non_array_tool_calls(self):
        with self.assertRaisesRegex(InvalidLLMResponse, "array|null"):
            self.adapter._parse_response(_Message({"content": "done", "tool_calls": {}}))

    def test_rejects_missing_tool_call_fields(self):
        with self.assertRaisesRegex(InvalidLLMResponse, "tool call fields"):
            self.adapter._parse_response(_Message({
                "content": "",
                "tool_calls": [{"id": "call-1"}],
            }))


class LiteLLMAdapterUsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_cached_prompt_tokens(self):
        adapter = object.__new__(LiteLLMAdapter)
        adapter._completion = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=_Message({"content": "done"}))],
            usage=SimpleNamespace(
                prompt_tokens=120,
                completion_tokens=3,
                prompt_tokens_details=SimpleNamespace(cached_tokens=96),
            ),
        ))

        result = await adapter.chat([], "model")

        self.assertEqual(result.usage.cached_input_tokens, 96)
        self.assertEqual(result.usage.uncached_input_tokens, 24)

    async def test_authentication_failure_has_a_specific_error(self):
        adapter = object.__new__(LiteLLMAdapter)
        adapter._completion = AsyncMock(side_effect=litellm.AuthenticationError(
            "invalid api key", "openai", "model"
        ))

        with self.assertRaisesRegex(LLMAuthenticationError, "invalid api key"):
            await adapter.chat([], "model")


class LiteLLMAdapterRequestTest(unittest.IsolatedAsyncioTestCase):
    def test_passes_router_config_without_interpreting_it(self):
        config = ModelConfig(
            active="logical-model",
            router={
                "model_list": [{
                    "model_name": "logical-model",
                    "litellm_params": {"model": "openai/provider-model"},
                }],
                "routing_strategy": "least-busy",
            },
        )

        with patch("helperme.llm.adapter.litellm.Router") as router:
            LiteLLMAdapter(config)

        router.assert_called_once_with(**config.router)

    async def test_calls_in_process_router(self):
        adapter = object.__new__(LiteLLMAdapter)
        adapter._read_attachment = None
        adapter._router = SimpleNamespace(acompletion=AsyncMock(return_value=object()))

        await adapter._completion("logical-model", [], None)

        adapter._router.acompletion.assert_awaited_once_with(
            model="logical-model",
            messages=[],
            tools=None,
            tool_choice=None,
        )
