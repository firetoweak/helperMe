import json
import unittest

from core.messages import Conversation
from core.model_call import LLMCallResult, LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.runtime_modes.router import RunMode, RuntimeModeRouter
from core.todos import TodoMode
from core.tools_runtime.run_runtime import RunRuntime
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


class RecordingLLMClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.seen_messages = []
        self.seen_tools = []

    def chat(self, messages, model, tools=None):
        self.seen_messages.append([message.copy() for message in messages])
        self.seen_tools.append(list(tools or []))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, LLMCallResult):
            return response
        return call_result(response)


class RunRuntimeRoutingTest(unittest.TestCase):
    @staticmethod
    def _conversation() -> Conversation:
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        return conversation

    @staticmethod
    def _route(mode: str, reason: str) -> LLMResponse:
        return LLMResponse(
            type="text",
            content=json.dumps(
                {"mode": mode, "reason": reason},
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _todo_initialization() -> LLMResponse:
        return LLMResponse(
            type="tool_calls",
            calls=[
                ToolCall(
                    "call-init",
                    "rewrite_todos",
                    json.dumps(
                        {
                            "objective": "完成复杂任务",
                            "reason": "初始化执行清单",
                            "todos": [
                                {
                                    "id": None,
                                    "content": "分析现状",
                                    "status": "pending",
                                },
                                {
                                    "id": None,
                                    "content": "形成结论",
                                    "status": "pending",
                                },
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        )

    @staticmethod
    def _complete_todos() -> LLMResponse:
        return LLMResponse(
            type="tool_calls",
            calls=[
                ToolCall(
                    "call-complete",
                    "rewrite_todos",
                    json.dumps(
                        {
                            "objective": "完成复杂任务",
                            "reason": "任务已经完成",
                            "todos": [
                                {
                                    "id": 1,
                                    "content": "分析现状",
                                    "status": "done",
                                },
                                {
                                    "id": 2,
                                    "content": "形成结论",
                                    "status": "done",
                                },
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        )

    @staticmethod
    def _runner(llm) -> RunRuntime:
        return RunRuntime(
            model_calls=model_call_service(llm),
            model="test-model",
            mode_router=RuntimeModeRouter(),
            runtime_modes={
                RunMode.PLAIN: PlainMode(),
                RunMode.TODO: TodoMode(),
            },
            context_preparation=context_preparation_service(),
            **runtime_tool_dependencies(),
        )

    def test_plain_route_skips_todo_initialization(self):
        llm = RecordingLLMClient(
            [
                self._route("plain", "可以直接回答"),
                LLMResponse(type="text", content="简单答案"),
            ]
        )

        result = self._runner(llm).run(
            self._conversation(),
            "1 + 1 等于几？",
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "简单答案")
        self.assertEqual(len(llm.seen_messages), 2)
        self.assertEqual(llm.seen_tools[0], [])
        self.assertNotIn("rewrite_todos", str(llm.seen_tools[1]))
        self.assertFalse(
            any(cp.reason == "todo_list_created" for cp in result.checkpoints)
        )

        routed = next(
            cp for cp in result.checkpoints
            if cp.reason == "runtime_mode_routed"
        )
        self.assertEqual(routed.data["mode"], "plain")
        self.assertEqual(routed.data["reason"], "可以直接回答")

    def test_todo_route_initializes_todos_before_agent_round(self):
        llm = RecordingLLMClient(
            [
                self._route("todo", "需要分析多个依赖步骤"),
                self._todo_initialization(),
                self._complete_todos(),
                LLMResponse(type="text", content="复杂任务完成"),
            ]
        )

        result = self._runner(llm).run(
            self._conversation(),
            "分析项目并给出修改建议",
            max_rounds=2,
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(len(llm.seen_messages), 4)
        self.assertEqual(llm.seen_tools[0], [])
        self.assertEqual(
            {tool["function"]["name"] for tool in llm.seen_tools[1]},
            {"rewrite_todos"},
        )
        reasons = [cp.reason for cp in result.checkpoints]
        self.assertIn("runtime_mode_routed", reasons)
        self.assertIn("todo_list_created", reasons)

        usage_stages = [
            cp.data["stage"]
            for cp in result.checkpoints
            if cp.reason == "llm_usage"
        ]
        self.assertEqual(
            usage_stages,
            [
                "routing",
                "todo_initialization",
                "agent_round",
                "agent_round",
            ],
        )

    def test_router_reads_full_conversation_and_does_not_write_decision_back(self):
        conversation = self._conversation()
        conversation.add_user("历史问题")
        conversation.add_assistant(
            LLMResponse(type="text", content="历史回答标记")
        )
        llm = RecordingLLMClient(
            [
                self._route("plain", "当前追问可以直接回答"),
                LLMResponse(type="text", content="追问答案"),
            ]
        )

        self._runner(llm).run(conversation, "简单追问")

        routing_messages = llm.seen_messages[0]
        self.assertTrue(
            any(
                message.get("content") == "历史回答标记"
                for message in routing_messages
            )
        )
        self.assertTrue(
            any(
                message.get("content") == "简单追问"
                for message in routing_messages
            )
        )
        self.assertFalse(
            any(
                "当前追问可以直接回答" in str(message.get("content"))
                for message in conversation.protocol_messages()
            )
        )

    def test_same_session_can_route_different_runs_to_different_modes(self):
        conversation = self._conversation()
        llm = RecordingLLMClient(
            [
                self._route("plain", "第一轮简单"),
                LLMResponse(type="text", content="第一轮答案"),
                self._route("todo", "第二轮复杂"),
                self._todo_initialization(),
                self._complete_todos(),
                LLMResponse(type="text", content="第二轮答案"),
            ]
        )
        runner = self._runner(llm)

        first = runner.run(conversation, "简单问题")
        second = runner.run(conversation, "现在完成一个复杂任务", max_rounds=2)

        first_route = next(
            cp for cp in first.checkpoints
            if cp.reason == "runtime_mode_routed"
        )
        second_route = next(
            cp for cp in second.checkpoints
            if cp.reason == "runtime_mode_routed"
        )
        self.assertEqual(first_route.data["mode"], "plain")
        self.assertEqual(second_route.data["mode"], "todo")

    def test_discussion_after_todo_run_can_return_to_plain_mode(self):
        conversation = self._conversation()
        llm = RecordingLLMClient(
            [
                self._route("todo", "明确要求完成复杂任务"),
                self._todo_initialization(),
                self._complete_todos(),
                LLMResponse(type="text", content="复杂任务完成"),
                self._route("plain", "当前只是在讨论优化方向"),
                LLMResponse(type="text", content="这个方向可以继续讨论"),
            ]
        )
        runner = self._runner(llm)

        first = runner.run(conversation, "实现并验证复杂修改", max_rounds=2)
        second = runner.run(
            conversation,
            "我觉得更好的优化方向是引入受限工作区。",
        )

        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertEqual(second.answer, "这个方向可以继续讨论")
        second_route = next(
            cp for cp in second.checkpoints
            if cp.reason == "runtime_mode_routed"
        )
        self.assertEqual(second_route.data["mode"], "plain")
        self.assertEqual(llm.seen_tools[4], [])
        self.assertNotIn("rewrite_todos", str(llm.seen_tools[5]))

    def test_invalid_route_falls_back_to_plain_in_same_run(self):
        llm = RecordingLLMClient(
            [
                LLMResponse(type="text", content="我觉得这是复杂任务"),
                LLMResponse(type="text", content="继续正常回答"),
            ]
        )
        conversation = self._conversation()

        result = self._runner(llm).run(conversation, "判断任务")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "继续正常回答")
        self.assertEqual(len(llm.seen_messages), 2)
        reasons = [cp.reason for cp in result.checkpoints]
        self.assertIn("invalid_runtime_mode_route", reasons)
        fallback = next(
            cp for cp in result.checkpoints
            if cp.reason == "runtime_mode_fallback"
        )
        self.assertEqual(fallback.data["from_mode"], None)
        self.assertEqual(fallback.data["to_mode"], "plain")
        self.assertNotIn(
            "我觉得这是复杂任务",
            [
                message.get("content")
                for message in conversation.protocol_messages()
            ],
        )

    def test_todo_activation_failure_falls_back_to_plain_in_same_run(self):
        initialization_text = "这个问题可以直接讨论，不需要创建 Todo。"
        llm = RecordingLLMClient(
            [
                self._route("todo", "判断为复杂任务"),
                LLMResponse(type="text", content=initialization_text),
                LLMResponse(type="text", content="按普通模式继续回答"),
            ]
        )
        conversation = self._conversation()

        result = self._runner(llm).run(conversation, "讨论一个复杂架构方向")

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "按普通模式继续回答")
        self.assertEqual(len(llm.seen_messages), 3)
        reasons = [cp.reason for cp in result.checkpoints]
        self.assertIn("invalid_todo_initialization", reasons)
        self.assertIn("runtime_mode_fallback", reasons)
        self.assertNotIn("todo_list_created", reasons)
        fallback = next(
            cp for cp in result.checkpoints
            if cp.reason == "runtime_mode_fallback"
        )
        self.assertEqual(fallback.data["from_mode"], "todo")
        self.assertEqual(fallback.data["to_mode"], "plain")
        self.assertNotIn(
            initialization_text,
            [
                message.get("content")
                for message in conversation.protocol_messages()
            ],
        )


if __name__ == "__main__":
    unittest.main()
