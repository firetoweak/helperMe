from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx2
from openai import AuthenticationError

from helperme.llm.api import LLMAuthenticationError
from helperme.llm.client import LLMClient
from helperme.llm.config import ModelConfig
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


class LLMClientRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_failure_has_a_specific_error(self):
        client = object.__new__(LLMClient)
        request = httpx2.Request("POST", "https://provider.example/chat")
        response = httpx2.Response(401, request=request)

        async def create(*_args):
            raise AuthenticationError(
                "invalid api key",
                response=response,
                body=None,
            )

        client.completions_create = create

        with self.assertRaisesRegex(
            LLMAuthenticationError,
            "invalid api key",
        ):
            await client.chat([], "model")

    def test_enables_sdk_transient_retries(self):
        config = ModelConfig(
            name="model",
            base_url="https://provider.example/v1",
            api_key="key",
            enable_thinking=False,
        )

        with patch("helperme.llm.client.httpx.AsyncClient") as http_client:
            with patch("helperme.llm.client.AsyncOpenAI") as openai_client:
                LLMClient(config)

        openai_client.assert_called_once_with(
            base_url=config.base_url,
            api_key=config.api_key,
            http_client=http_client.return_value,
            max_retries=2,
        )

    async def test_passes_thinking_switch_to_provider(self):
        client = object.__new__(LLMClient)
        client._enable_thinking = True
        create = AsyncMock(return_value=object())
        client.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            )
        )

        await client.completions_create("model", [], None)

        create.assert_awaited_once_with(
            model="model",
            messages=[],
            tools=None,
            tool_choice=None,
            extra_body={"enable_thinking": True},
        )
