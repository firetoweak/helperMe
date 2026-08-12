import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from pathlib import Path

from core.context import ModelContext, make_budget_assessment
from core.model_call import InvalidLLMResponse, LLMResponse, ToolCall
from core.model_call.client import LLMClient
from core.model_call.config import load_app_config, load_model_config
from core.model_call.service import (
    ModelCallBlocked,
    ModelCallRequest,
    ModelCallService,
)
from core.model_call.types import LLMCallResult, LLMUsage


class LLMResponseContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_response_requires_non_empty_content(self):
        for content in ("", "   "):
            with self.subTest(content=content):
                with self.assertRaises(InvalidLLMResponse) as raised:
                    LLMResponse(content=content)

                self.assertEqual(raised.exception.code, "empty_model_response")

        with self.assertRaises(InvalidLLMResponse) as raised:
            LLMResponse(content=None)
        self.assertEqual(raised.exception.code, "invalid_llm_response")

    async def test_tool_calls_response_requires_non_empty_calls(self):
        for calls in (None, []):
            with self.subTest(calls=calls):
                with self.assertRaises(InvalidLLMResponse):
                    LLMResponse(calls=calls)

    async def test_valid_response_variants(self):
        text = LLMResponse(content="done")
        tool_calls = LLMResponse(
            calls=(ToolCall(id="call-1", name="read_file", arguments="{}"),),
        )
        mixed = LLMResponse(
            content="我先读取关键文件。",
            calls=(ToolCall(id="call-2", name="read_file", arguments="{}"),),
        )

        self.assertEqual(text.content, "done")
        self.assertEqual(tool_calls.calls[0].id, "call-1")
        self.assertEqual(mixed.content, "我先读取关键文件。")
        self.assertEqual(mixed.calls[0].id, "call-2")

    async def test_client_parser_rejects_empty_sdk_response(self):
        response = SimpleNamespace(tool_calls=None, content=None)

        with self.assertRaises(InvalidLLMResponse) as raised:
            LLMClient._parse_response(None, response)

        self.assertEqual(raised.exception.code, "empty_model_response")

    async def test_client_parser_preserves_content_with_tool_calls(self):
        call = SimpleNamespace(
            id="call-1",
            function=SimpleNamespace(name="read_file", arguments="{}"),
        )
        response = SimpleNamespace(
            tool_calls=[call],
            content="我先读取关键实现。",
        )

        parsed = LLMClient._parse_response(None, response)

        self.assertEqual(parsed.content, "我先读取关键实现。")
        self.assertEqual(parsed.calls, (ToolCall("call-1", "read_file", "{}"),))

    async def test_client_returns_response_with_real_usage(self):
        client = object.__new__(LLMClient)
        client.completions_create = AsyncMock(
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

        result = await client.chat([], "test-model", tools=None)

        self.assertEqual(result.response.content, "done")
        self.assertEqual(result.usage.input_tokens, 120)
        self.assertEqual(result.usage.output_tokens, 30)
        self.assertEqual(result.usage.total_tokens, 150)


class ModelConfigTest(unittest.IsolatedAsyncioTestCase):
    async def test_loads_model_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model_config.yaml"
            path.write_text(
                "model:\n"
                "  name: test-model\n"
                "  base_url: https://example.test/v1\n"
                "  api_key: test-key\n"
                "workspace:\n"
                "  root: C:\\\\work\\\\agent\n"
                "  full_access: true\n"
                "runtime:\n"
                "  max_rounds: 80\n"
                "  max_goal_turns: 6\n"
                "  model_context_limit: 200000\n"
                "  input_budget_ratio: 0.85\n",
                encoding="utf-8",
            )

            config = load_app_config(path)

        self.assertEqual(config.model.name, "test-model")
        self.assertEqual(config.model.base_url, "https://example.test/v1")
        self.assertEqual(config.model.api_key, "test-key")
        self.assertEqual(config.workspace.root, Path(r"C:\work\agent"))
        self.assertTrue(config.workspace.full_access)
        self.assertEqual(config.runtime.max_rounds, 80)
        self.assertEqual(config.runtime.max_goal_turns, 6)
        self.assertEqual(config.runtime.model_context_limit, 200_000)
        self.assertEqual(config.runtime.input_budget_ratio, 0.85)

    async def test_rejects_invalid_runtime_hyperparameters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model_config.yaml"
            path.write_text(
                "model:\n"
                "  name: test-model\n"
                "  base_url: https://example.test/v1\n"
                "  api_key: test-key\n"
                "workspace:\n"
                "  root: C:\\\\work\\\\agent\n"
                "  full_access: false\n"
                "runtime:\n"
                "  max_rounds: 0\n"
                "  max_goal_turns: 6\n"
                "  model_context_limit: 200000\n"
                "  input_budget_ratio: 0.9\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "runtime.max_rounds"):
                load_app_config(path)

    async def test_rejects_missing_required_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model_config.yaml"
            path.write_text(
                "model:\n  name: test-model\n  base_url: ''\n  api_key: key\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "model.base_url"):
                load_model_config(path)


class ModelCallServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_budget_exceeded_does_not_call_model(self):
        llm_client = Mock()
        llm_client.chat = AsyncMock()
        context_budget = Mock()
        assessment = make_budget_assessment(
            estimated_input_tokens=801,
            input_budget_tokens=750,
        )
        context_budget.assess.return_value = assessment
        request = ModelCallRequest(
            context=ModelContext(messages=[]),
            tools=[],
        )

        outcome = await ModelCallService(
            llm_client=llm_client,
            context_budget=context_budget,
        ).call(request, "test-model")

        self.assertIsInstance(outcome, ModelCallBlocked)
        self.assertIs(outcome.assessment, assessment)
        llm_client.chat.assert_not_called()
        context_budget.observe_actual_usage.assert_not_called()

    async def test_success_calibrates_with_real_input_usage(self):
        llm_client = Mock()
        llm_client.chat = AsyncMock()
        context_budget = Mock()
        context_budget.assess.return_value = make_budget_assessment(
            estimated_input_tokens=700,
            input_budget_tokens=750,
        )
        call_result = LLMCallResult(
            response=LLMResponse(content="done"),
            usage=LLMUsage(input_tokens=680, output_tokens=20),
        )
        llm_client.chat.return_value = call_result
        context = ModelContext(
            messages=[{"role": "user", "content": "hello"}]
        )
        tools = [{"type": "function"}]
        request = ModelCallRequest(context=context, tools=tools)

        outcome = await ModelCallService(
            llm_client=llm_client,
            context_budget=context_budget,
        ).call(request, "test-model")

        self.assertIs(outcome, call_result)
        llm_client.chat.assert_called_once_with(
            context.messages,
            "test-model",
            tools,
        )
        context_budget.observe_actual_usage.assert_called_once_with(
            context,
            tools,
            680,
        )


if __name__ == "__main__":
    unittest.main()
