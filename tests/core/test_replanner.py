import json
import unittest

from core.model_call import InvalidLLMResponse, LLMResponse, ToolCall
from core.planning import Plan, PlanStep
from core.planning.replanner import (
    build_replanner_instruction,
    InvalidReplanResponse,
    ReplanDecision,
    parse_replan_response,
    replan,
)
from core.tools_runtime.tools_state import ToolStep


class ReplannerTest(unittest.TestCase):
    @staticmethod
    def _plan() -> Plan:
        return Plan(
            goal="完成任务",
            steps=[
                PlanStep(id=1, text="分析", status="done"),
                PlanStep(id=2, text="调用接口", status="doing"),
                PlanStep(id=3, text="处理结果", status="pending"),
            ],
        )

    @staticmethod
    def _failed_step() -> ToolStep:
        return ToolStep(
            call_id="call-1",
            name="missing_tool",
            arguments="{}",
            result={
                "ok": False,
                "code": "TOOL_NOT_FOUND",
                "data": None,
                "error": "Tool missing_tool not found",
                "hint": "换用已注册工具",
            },
        )

    def test_build_instruction_contains_plan_and_failure(self):
        instruction = build_replanner_instruction(
            self._plan(),
            [self._failed_step()],
        )

        self.assertIn("replanner", instruction)
        self.assertIn("revision=1", instruction)
        self.assertIn("调用接口", instruction)
        self.assertIn("tool=missing_tool", instruction)
        self.assertIn("code=TOOL_NOT_FOUND", instruction)

    def test_replan_parses_response_without_mutating_plan(self):
        plan = self._plan()
        before = plan.to_dict()

        decision = replan(
            LLMResponse(
                type="text",
                content=(
                    '{"action":"revise","reason":"原工具不存在",'
                    '"steps":["检查可用工具","继续任务"]}'
                ),
            )
        )

        self.assertEqual(decision.action, "revise")
        self.assertEqual(decision.steps, ["检查可用工具", "继续任务"])
        self.assertEqual(plan.to_dict(), before)

    def test_replan_rejects_non_text_response(self):
        with self.assertRaises(InvalidLLMResponse) as raised:
            replan(
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall(id="call-1", name="tool", arguments="{}")],
                )
            )

        self.assertEqual(raised.exception.code, "invalid_replan_response")

    def test_replan_invalid_json_exposes_raw_response(self):
        with self.assertRaises(InvalidLLMResponse) as raised:
            replan(LLMResponse(type="text", content="不是合法 JSON"))

        self.assertEqual(raised.exception.code, "invalid_replan_response")
        self.assertIn("raw_response='不是合法 JSON'", str(raised.exception))

    def test_parse_keep_decision(self):
        decision = parse_replan_response(
            '{"action":"keep","reason":"原计划仍然有效"}'
        )
        self.assertEqual(
            decision,
            ReplanDecision(
                action="keep",
                reason="原计划仍然有效",
                steps=[],
            ),
        )

    def test_parse_keep_accepts_empty_steps(self):
        decision = parse_replan_response(
            '{"action":"keep","reason":"继续执行","steps":[]}'
        )
        self.assertEqual(decision.steps, [])

    def test_parse_revise_decision_trims_reason_and_steps(self):
        decision = parse_replan_response(
            """
            {
                "action": "revise",
                "reason": "  原接口不存在  ",
                "steps": ["  检查本地数据  ", "验证结果"]
            }
            """
        )
        self.assertEqual(
            decision,
            ReplanDecision(
                action="revise",
                reason="原接口不存在",
                steps=["检查本地数据", "验证结果"],
            ),
        )

    def test_invalid_json_or_non_object_fails(self):
        for content in ("不是 JSON", "[]", '"keep"'):
            with self.subTest(content=content):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(content)

    def test_unknown_or_missing_action_fails(self):
        for content in (
            '{"reason": "缺少 action"}',
            '{"action": "retry", "reason": "未知 action"}',
        ):
            with self.subTest(content=content):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(content)

    def test_missing_or_blank_reason_fails(self):
        for content in (
            '{"action": "keep"}',
            '{"action": "keep", "reason": "   "}',
            '{"action": "keep", "reason": 123}',
        ):
            with self.subTest(content=content):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(content)

    def test_keep_with_steps_fails(self):
        with self.assertRaises(InvalidReplanResponse):
            parse_replan_response(
                '{"action":"keep","reason":"继续",'
                '"steps":["不应提供新步骤"]}'
            )

    def test_revise_requires_one_to_six_non_empty_steps(self):
        invalid_steps = (
            None,
            [],
            ["步骤1", "步骤2", "步骤3", "步骤4", "步骤5", "步骤6", "步骤7"],
            ["有效步骤", "   "],
            ["有效步骤", 123],
        )
        for steps in invalid_steps:
            payload = {"action": "revise", "reason": "需要修改"}
            if steps is not None:
                payload["steps"] = steps
            with self.subTest(steps=steps):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
