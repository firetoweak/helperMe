import unittest
from unittest.mock import Mock

from core.context import ContextState
from core.messages import Conversation
from core.planning import (
    create_plan,
    format_plan_for_model,
    InvalidPlanResponse,
    parse_plan_response,
)
from core.model_call import LLMResponse
from core.model_call.service import ModelCallRequest
from tests.core.llm_test_support import call_result, context_preparation_service


class PlannerTest(unittest.TestCase):
    def test_create_and_format_plan(self):
        model_calls = Mock()
        model_calls.call.return_value = call_result(
            LLMResponse(
                type="text",
                content='{"goal": "帮我分析项目", "steps": ["读取项目", "分析结构"]}',
            )
        )
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("帮我分析项目")

        plan_result = create_plan(
            conversation,
            context_preparation_service(),
            ContextState(),
            model_calls,
            "test-model",
        )
        plan = plan_result.plan
        text = format_plan_for_model(plan)

        self.assertEqual(plan.goal, "帮我分析项目")
        self.assertEqual(len(plan.steps), 2)
        self.assertIn("当前执行计划", text)
        self.assertIn("[pending]", text)

    def test_create_plan_propagates_llm_failure(self):
        model_calls = Mock()
        model_calls.call.side_effect = RuntimeError("planner unavailable")
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("帮我分析项目")

        with self.assertRaisesRegex(RuntimeError, "planner unavailable"):
            create_plan(
                conversation,
                context_preparation_service(),
                ContextState(),
                model_calls,
                "test-model",
            )

    def test_create_plan_projects_full_conversation_without_tools(self):
        model_calls = Mock()
        model_calls.call.return_value = call_result(
            LLMResponse(
                type="text",
                content='{"goal": "继续分析", "steps": ["读取历史", "继续任务"]}',
            )
        )
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("第一轮问题")
        conversation.add_assistant(LLMResponse(type="text", content="第一轮回答"))
        conversation.add_user("继续分析")

        create_plan(
            conversation,
            context_preparation_service(),
            ContextState(),
            model_calls,
            "test-model",
        )

        request, model = model_calls.call.call_args.args

        self.assertIsInstance(request, ModelCallRequest)
        self.assertEqual(model, "test-model")
        self.assertEqual(request.tools, [])
        self.assertIn("只返回 JSON", request.context.messages[0]["content"])
        self.assertEqual(
            request.context.messages[1:],
            conversation.protocol_messages()[1:],
        )
        self.assertEqual(
            conversation.records[0].payload["content"],
            "system prompt",
        )

    def test_parse_valid_plan_response(self):
        plan = parse_plan_response(
            '{"goal": "分析项目结构", "steps": ["读取文件", "分析职责", "给出建议"]}'
        )

        self.assertEqual(plan.goal, "分析项目结构")
        self.assertEqual(
            [step.text for step in plan.steps],
            ["读取文件", "分析职责", "给出建议"],
        )
        self.assertEqual([step.id for step in plan.steps], [1, 2, 3])
        self.assertTrue(all(step.status == "pending" for step in plan.steps))

    def test_parse_invalid_json_fails(self):
        with self.assertRaises(InvalidPlanResponse):
            parse_plan_response("不是 JSON")

    def test_parse_too_few_steps_fails(self):
        with self.assertRaises(InvalidPlanResponse):
            parse_plan_response('{"goal": "目标", "steps": ["只有一步"]}')

    def test_parse_too_many_steps_fails(self):
        with self.assertRaises(InvalidPlanResponse):
            parse_plan_response(
                '{"goal": "目标", "steps": ["1", "2", "3", "4", "5", "6", "7"]}'
            )

    def test_parse_invalid_goal_or_step_fails(self):
        invalid_responses = (
            '{"goal": "", "steps": ["1", "2"]}',
            '{"goal": "目标", "steps": ["1", null]}',
            '{"goal": "目标", "steps": ["1", ""]}',
        )

        for content in invalid_responses:
            with self.subTest(content=content):
                with self.assertRaises(InvalidPlanResponse):
                    parse_plan_response(content)


if __name__ == "__main__":
    unittest.main()
