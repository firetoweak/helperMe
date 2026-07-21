import unittest

from core.model_call import InvalidLLMResponse, LLMResponse, ToolCall
from core.runtime_modes.router import (
    InvalidRouteResponse,
    RouteDecision,
    RunMode,
    RuntimeModeRouter,
    parse_route_response,
)


class RuntimeModeRouterTest(unittest.TestCase):
    def test_prompt_routes_execution_complexity_not_session_identity(self):
        prompt = RuntimeModeRouter().system_prompt

        self.assertIn("本次 Run", prompt)
        self.assertIn("完整 Conversation", prompt)
        self.assertIn("plain", prompt)
        self.assertIn("todo", prompt)
        self.assertIn("不确定", prompt)
        self.assertNotIn("整个 Session", prompt)

    def test_prompt_does_not_treat_discussion_as_implementation_authority(self):
        prompt = RuntimeModeRouter().system_prompt

        self.assertIn("最后一条用户消息", prompt)
        self.assertIn("讨论、评价、解释或提出方案", prompt)
        self.assertIn("推断为授权实施", prompt)
        self.assertIn("明确要求执行", prompt)
        self.assertIn("是否授权执行不明确", prompt)

    def test_accepts_plain_and_todo_decisions(self):
        cases = (
            (
                '{"mode":"plain","reason":"可以直接回答"}',
                RouteDecision(RunMode.PLAIN, "可以直接回答"),
            ),
            (
                '{"mode":"todo","reason":"需要分析、修改和验证"}',
                RouteDecision(RunMode.TODO, "需要分析、修改和验证"),
            ),
        )

        for content, expected in cases:
            with self.subTest(content=content):
                self.assertEqual(parse_route_response(content), expected)

    def test_parser_trims_reason(self):
        decision = parse_route_response(
            '{"mode":"todo","reason":"  需要多个依赖步骤  "}'
        )

        self.assertEqual(decision.reason, "需要多个依赖步骤")

    def test_parser_rejects_invalid_json_shape_mode_and_reason(self):
        invalid_responses = (
            "not json",
            "[]",
            '{"mode":"plain"}',
            '{"mode":"plain","reason":"   "}',
            '{"mode":"complex","reason":"未知模式"}',
            '{"mode":"todo","reason":123}',
            '{"mode":"todo","reason":"复杂","extra":true}',
        )

        for content in invalid_responses:
            with self.subTest(content=content):
                with self.assertRaises(InvalidRouteResponse):
                    parse_route_response(content)

    def test_router_wraps_invalid_text_with_raw_response(self):
        with self.assertRaises(InvalidLLMResponse) as raised:
            RuntimeModeRouter().accept_response(
                LLMResponse(type="text", content="先分析一下")
            )

        self.assertEqual(raised.exception.code, "invalid_runtime_mode_route")
        self.assertIn("raw_response='先分析一下'", str(raised.exception))

    def test_router_rejects_tool_calls(self):
        with self.assertRaises(InvalidLLMResponse) as raised:
            RuntimeModeRouter().accept_response(
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("call-1", "read_file", "{}")],
                )
            )

        self.assertEqual(raised.exception.code, "invalid_runtime_mode_route")


if __name__ == "__main__":
    unittest.main()
