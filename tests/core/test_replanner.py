import json
import unittest
from unittest.mock import Mock

from core.context import ContextState, make_budget_assessment
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.model_call.service import ModelCallBlocked, ModelCallRequest
from core.planning import Plan, PlanStep
from core.planning.replanner import (
    InvalidReplanResponse,
    ReplanCallBlocked,
    ReplanDecision,
    parse_replan_response,
    replan,
)
from core.tools_runtime.tools_state import ToolStep
from tests.core.llm_test_support import call_result, context_preparation_service


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
    def _conversation() -> Conversation:
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("完成任务")
        return conversation

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

    def test_replan_calls_model_with_plan_and_failure_without_mutating_plan(self):
        model_calls = Mock()
        model_calls.call.return_value = call_result(
            LLMResponse(
                type="text",
                content=(
                    '{"action":"revise","reason":"原工具不存在",'
                    '"steps":["检查可用工具","继续任务"]}'
                ),
            ),
            input_tokens=101,
            output_tokens=11,
        )
        conversation = self._conversation()
        plan = self._plan()
        plan_before = plan.to_dict()

        outcome = replan(
            conversation=conversation,
            plan=plan,
            failed_steps=[self._failed_step()],
            context_preparation=context_preparation_service(),
            context_state=ContextState(),
            model_calls=model_calls,
            model="test-model",
        )

        self.assertEqual(outcome.decision.action, "revise")
        self.assertEqual(outcome.decision.steps, ["检查可用工具", "继续任务"])
        self.assertEqual(outcome.usage.input_tokens, 101)
        self.assertEqual(outcome.usage.output_tokens, 11)
        self.assertEqual(plan.to_dict(), plan_before)

        request, model = model_calls.call.call_args.args
        self.assertIsInstance(request, ModelCallRequest)
        self.assertEqual(model, "test-model")
        self.assertEqual(request.tools, [])
        instruction = request.context.messages[0]["content"]
        self.assertIn("replanner", instruction)
        self.assertIn("revision=1", instruction)
        self.assertIn("2. [doing] 调用接口", instruction)
        self.assertIn("tool=missing_tool", instruction)
        self.assertIn("code=TOOL_NOT_FOUND", instruction)
        self.assertIn("error=Tool missing_tool not found", instruction)
        self.assertEqual(
            request.context.messages[1:],
            conversation.protocol_messages()[1:],
        )

    def test_replan_returns_keep_decision(self):
        model_calls = Mock()
        model_calls.call.return_value = call_result(
            LLMResponse(
                type="text",
                content='{"action":"keep","reason":"原计划仍然有效"}',
            )
        )

        outcome = replan(
            conversation=self._conversation(),
            plan=self._plan(),
            failed_steps=[self._failed_step()],
            context_preparation=context_preparation_service(),
            context_state=ContextState(),
            model_calls=model_calls,
            model="test-model",
        )

        self.assertEqual(outcome.decision.action, "keep")
        self.assertEqual(outcome.decision.steps, [])

    def test_replan_propagates_model_blocked_result(self):
        model_calls = Mock()
        assessment = make_budget_assessment(
            estimated_input_tokens=820,
            input_budget_tokens=750,
        )
        model_calls.call.return_value = ModelCallBlocked(assessment)

        outcome = replan(
            conversation=self._conversation(),
            plan=self._plan(),
            failed_steps=[self._failed_step()],
            context_preparation=context_preparation_service(),
            context_state=ContextState(),
            model_calls=model_calls,
            model="test-model",
        )

        self.assertIsInstance(outcome, ReplanCallBlocked)
        self.assertIs(outcome.blocked.assessment, assessment)

    def test_replan_rejects_non_text_model_response(self):
        model_calls = Mock()
        model_calls.call.return_value = call_result(
            LLMResponse(
                type="tool_calls",
                calls=[
                    ToolCall(
                        id="call-unexpected",
                        name="unexpected_tool",
                        arguments="{}",
                    )
                ],
            )
        )

        with self.assertRaises(InvalidReplanResponse):
            replan(
                conversation=self._conversation(),
                plan=self._plan(),
                failed_steps=[self._failed_step()],
                context_preparation=context_preparation_service(),
                context_state=ContextState(),
                model_calls=model_calls,
                model="test-model",
            )

    def test_parse_keep_decision(self):
        decision = parse_replan_response(
            '{"action": "keep", "reason": "失败不影响原计划"}'
        )

        self.assertEqual(
            decision,
            ReplanDecision(
                action="keep",
                reason="失败不影响原计划",
                steps=[],
            ),
        )

    def test_parse_keep_accepts_empty_steps(self):
        decision = parse_replan_response(
            """
            {
                "action": "keep",
                "reason": "原计划仍然有效",
                "steps": []
            }
            """
        )

        self.assertEqual(
            decision,
            ReplanDecision(
                action="keep",
                reason="原计划仍然有效",
                steps=[],
            ),
        )

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
                """
                {
                    "action": "keep",
                    "reason": "继续原计划",
                    "steps": ["不应提供新步骤"]
                }
                """
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
            if steps is None:
                content = '{"action": "revise", "reason": "需要修改"}'
            else:
                content = json.dumps(
                    {
                        "action": "revise",
                        "reason": "需要修改",
                        "steps": steps,
                    },
                    ensure_ascii=False,
                )

            with self.subTest(steps=steps):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(content)


if __name__ == "__main__":
    unittest.main()
