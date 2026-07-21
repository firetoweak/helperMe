import json
import unittest
from unittest.mock import Mock, patch

from core.context import make_budget_assessment
from core.messages import Conversation
from core.model_call import LLMCallResult, LLMResponse, ToolCall
from core.model_call.client import LLMContextLengthError, LLMTransientError
from core.model_call.service import ModelCallBlocked
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


class RunRuntimeTodosTest(unittest.TestCase):
    @staticmethod
    def _conversation() -> Conversation:
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        return conversation

    @staticmethod
    def _runner(llm) -> RunRuntime:
        return RunRuntime(
            model_call_service(llm),
            "test-model",
            TodoMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

    @staticmethod
    def _initial_response() -> LLMResponse:
        return LLMResponse(
            type="tool_calls",
            calls=[
                ToolCall(
                    "call-init",
                    "rewrite_todos",
                    json.dumps(
                        {
                            "objective": "完成任务",
                            "reason": "初始化执行清单",
                            "todos": [
                                {
                                    "id": None,
                                    "content": "读取信息",
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
    def _rewrite_call(*, done: bool = True) -> LLMResponse:
        status = "done" if done else "pending"
        return LLMResponse(
            type="tool_calls",
            calls=[
                ToolCall(
                    "call-rewrite",
                    "rewrite_todos",
                    json.dumps(
                        {
                            "objective": "完成任务",
                            "reason": "同步执行结果",
                            "todos": [
                                {
                                    "id": 1,
                                    "content": "读取信息",
                                    "status": status,
                                    "note": "已读取" if done else None,
                                },
                                {
                                    "id": 2,
                                    "content": "形成结论",
                                    "status": status,
                                    "note": "已完成" if done else None,
                                },
                            ],
                        },
                        ensure_ascii=False,
                    ),
                )
            ],
        )

    def test_initial_generator_is_read_only_and_todo_is_runtime_instruction(self):
        llm = RecordingLLMClient(
            [self._initial_response(), self._rewrite_call(), LLMResponse("text", "完成")]
        )

        result = self._runner(llm).run(self._conversation(), "完成任务")

        self.assertEqual(result.status, "completed")
        initialization_tool_names = {
            tool["function"]["name"] for tool in llm.seen_tools[0]
        }
        self.assertEqual(initialization_tool_names, {"rewrite_todos"})
        self.assertIn("Todo 初始化阶段", llm.seen_messages[0][0]["content"])
        self.assertIn("当前 Todo：", llm.seen_messages[1][0]["content"])
        self.assertNotIn("revision=", llm.seen_messages[1][0]["content"])
        self.assertNotIn("sync=", llm.seen_messages[1][0]["content"])
        runtime_tool_names = {
            tool["function"]["name"] for tool in llm.seen_tools[1]
        }
        self.assertIn("rewrite_todos", runtime_tool_names)
        created = next(
            cp for cp in result.checkpoints if cp.reason == "todo_list_created"
        )
        self.assertEqual(created.data["todo_list"]["revision"], 1)
        self.assertEqual(
            result.checkpoints[-1].data["todo_list"]["phase"],
            "completed",
        )

    def test_dirty_todos_do_not_block_execution_but_block_final_answer(self):
        llm = RecordingLLMClient(
            [
                self._initial_response(),
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("call-read", "missing_tool", "{}")],
                ),
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("call-read-2", "missing_tool", "{}")],
                ),
                LLMResponse(type="text", content="过早回答"),
                self._rewrite_call(),
                LLMResponse(type="text", content="最终回答"),
            ]
        )
        conversation = self._conversation()

        result = self._runner(llm).run(conversation, "完成任务", max_rounds=5)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "最终回答")
        self.assertIn(
            "外部工具结果尚未同步",
            conversation.protocol_messages()[-4]["content"],
        )

    def test_clean_but_unresolved_todos_block_final_answer(self):
        llm = RecordingLLMClient(
            [
                self._initial_response(),
                LLMResponse(type="text", content="过早回答"),
                self._rewrite_call(),
                LLMResponse(type="text", content="最终回答"),
            ]
        )
        conversation = self._conversation()

        result = self._runner(llm).run(conversation, "完成任务", max_rounds=3)

        self.assertEqual(result.status, "completed")
        self.assertTrue(
            any(
                message.get("role") == "user"
                and "未结束事项" in str(message.get("content"))
                for message in conversation.protocol_messages()
            )
        )

    def test_invalid_initial_generation_fails_at_boundary(self):
        llm = RecordingLLMClient([LLMResponse(type="text", content="not json")])

        result = self._runner(llm).run(self._conversation(), "完成任务")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.final_reason, "invalid_todo_initialization")
        self.assertIn("raw_response=LLMResponse", result.answer)

    def test_reused_runtime_creates_an_independent_todo_state_per_run(self):
        llm = RecordingLLMClient(
            [
                self._initial_response(),
                self._rewrite_call(),
                LLMResponse(type="text", content="第一次完成"),
                self._initial_response(),
                self._rewrite_call(),
                LLMResponse(type="text", content="第二次完成"),
            ]
        )
        runner = self._runner(llm)

        first = runner.run(self._conversation(), "第一次任务")
        second = runner.run(self._conversation(), "第二次任务")

        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        for result in (first, second):
            created = next(
                cp for cp in result.checkpoints
                if cp.reason == "todo_list_created"
            )
            self.assertEqual(created.data["todo_list"]["revision"], 1)
            self.assertEqual(
                created.data["todo_list"]["phase"],
                "active",
            )

    def test_initialization_budget_failure_reports_todo_stage(self):
        model_calls = Mock()
        model_calls.call.return_value = ModelCallBlocked(
            make_budget_assessment(820, 750)
        )
        result = RunRuntime(
            model_calls,
            "test-model",
            TodoMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        ).run(self._conversation(), "完成任务")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(
            result.checkpoints[-1].data["stage"],
            "todo_initialization",
        )

    def test_initialization_model_hard_limit_reports_todo_stage(self):
        model_calls = Mock()
        model_calls.call.side_effect = LLMContextLengthError(
            "maximum context length exceeded"
        )
        result = RunRuntime(
            model_calls,
            "test-model",
            TodoMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        ).run(self._conversation(), "完成任务")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.final_reason, "context_length_exceeded")
        self.assertEqual(
            result.checkpoints[-1].data["stage"],
            "todo_initialization",
        )

    @patch("core.tools_runtime.run_runtime.time.sleep")
    def test_initialization_transient_failure_reuses_retry_chain(self, sleep):
        llm = RecordingLLMClient(
            [
                LLMTransientError("todo generator timeout"),
                self._initial_response(),
                self._rewrite_call(),
                LLMResponse(type="text", content="完成"),
            ]
        )

        result = self._runner(llm).run(self._conversation(), "完成任务")

        self.assertEqual(result.status, "completed")
        retry = next(
            cp for cp in result.checkpoints if cp.reason == "llm_retry"
        )
        self.assertEqual(retry.data["stage"], "todo_initialization")
        self.assertIsNone(retry.data["round_index"])
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
