import unittest
from unittest.mock import Mock

import tools  # noqa: F401
from core.agent_application import AgentApplication
from core.context import ContextManager
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.session_runner import SessionRuntime
from core.session_state import SessionEventType, SessionStatus
from core.tools_runtime.run_runtime import RunRuntime, RunStatus
from core.tools_runtime.tools_protocol import validate_tool_message_chain
from tests.core.llm_test_support import (
    call_result,
    model_call_service,
    runtime_tool_dependencies,
)


SUCCESS = {
    "ok": True,
    "code": "OK",
    "data": None,
    "error": None,
    "hint": None,
}


class AgentApplicationSessionRuntimeTest(unittest.TestCase):
    def _build_application(self, llm_client: Mock):
        session_runtime = SessionRuntime(
            RunRuntime(
                model_calls=model_call_service(llm_client),
                model="test-model",
                runtime_mode=PlainMode(),
                context_manager=ContextManager(),
                **runtime_tool_dependencies(SUCCESS),
            )
        )
        application = AgentApplication(
            session_runtime=session_runtime,
            system_prompt="system prompt",
        )
        application.create_session("session-1")
        return application, session_runtime

    def test_application_starts_and_resumes_through_session_runtime(
        self,
    ):
        llm_client = Mock()
        responses = iter(
            (
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("call-1", "demo", "{}")],
                ),
                LLMResponse(type="text", content="任务已完成"),
            )
        )
        application = None

        def chat(messages, model, tools=None):
            response = next(responses)
            if response.type == "tool_calls":
                application.request_interrupt("session-1", "等待继续")
            return call_result(response)

        llm_client.chat.side_effect = chat
        application, session_runtime = self._build_application(llm_client)
        session = session_runtime.sessions["session-1"]

        interrupted = application.start("session-1", "run-1", "开始任务")

        self.assertEqual(interrupted.result.status, RunStatus.INTERRUPTED)
        self.assertEqual(session.status, SessionStatus.INTERRUPTED)
        self.assertTrue(validate_tool_message_chain(session.conversation.messages).ok)

        completed = application.resume("session-1", "run-2", "继续执行")

        self.assertEqual(completed.result.answer, "任务已完成")
        self.assertEqual(completed.result.status, RunStatus.COMPLETED)
        self.assertEqual(session.status, SessionStatus.COMPLETED)
        self.assertEqual(len(session.run_records), 2)
        self.assertEqual(
            [event.kind for event in session.events],
            [
                SessionEventType.CREATED,
                SessionEventType.STARTED,
                SessionEventType.INTERRUPTED,
                SessionEventType.RESUMED,
                SessionEventType.COMPLETED,
            ],
        )
        self.assertTrue(validate_tool_message_chain(session.conversation.messages).ok)

    def test_application_starts_new_run_in_same_session_after_completed(self):
        llm_client = Mock()
        llm_client.chat.side_effect = (
            call_result(LLMResponse(type="text", content="第一轮完成")),
            call_result(LLMResponse(type="text", content="第二轮完成")),
        )
        application, session_runtime = self._build_application(llm_client)
        session = session_runtime.sessions["session-1"]

        first = application.start("session-1", "run-1", "第一轮")
        second = application.start("session-1", "run-2", "第二轮")

        self.assertEqual(first.result.answer, "第一轮完成")
        self.assertEqual(second.result.answer, "第二轮完成")
        self.assertEqual(len(session.run_records), 2)
        self.assertEqual(
            [event.kind for event in session.events],
            [
                SessionEventType.CREATED,
                SessionEventType.STARTED,
                SessionEventType.COMPLETED,
                SessionEventType.STARTED,
                SessionEventType.COMPLETED,
            ],
        )


if __name__ == "__main__":
    unittest.main()
