import unittest
from unittest.mock import patch

from core.agent import Agent
from core.messages import LLMResponse, ToolCall
from core.session_state import SessionEventType, SessionStatus
from core.tools_runtime.tools_protocol import validate_tool_message_chain
from core.tools_runtime.tools_runner import RunStatus


SUCCESS = {
    "ok": True,
    "code": "OK",
    "data": None,
    "error": None,
    "hint": None,
}


class AgentSessionRuntimeTest(unittest.TestCase):
    @patch("core.tools_runtime.tools_runner.execute_tool", return_value=SUCCESS)
    @patch("core.agent.LLMClient")
    def test_agent_starts_and_resumes_through_session_runtime(
        self,
        llm_client_class,
        _execute_tool,
    ):
        agent = None
        chat_calls = 0
        responses = iter(
            (
                LLMResponse(
                    type="tool_calls",
                    calls=[ToolCall("call-1", "demo", "{}")],
                ),
                LLMResponse(type="text", content="任务已完成"),
            )
        )

        def chat(messages, model, tools=None):
            nonlocal chat_calls
            chat_calls += 1
            if chat_calls == 1:
                agent.request_interrupt("等待继续")
            return next(responses)

        llm_client_class.return_value.chat.side_effect = chat
        agent = Agent(model="test-model")

        interrupted_answer = agent.run("开始任务")

        self.assertTrue(interrupted_answer)
        self.assertEqual(agent.session.status, SessionStatus.INTERRUPTED)
        self.assertEqual(agent.last_result.status, RunStatus.INTERRUPTED)
        self.assertIs(agent.conversation, agent.session.conversation)
        self.assertTrue(validate_tool_message_chain(agent.conversation.messages).ok)

        completed_answer = agent.run("继续执行")

        self.assertEqual(completed_answer, "任务已完成")
        self.assertEqual(agent.session.status, SessionStatus.COMPLETED)
        self.assertEqual(agent.last_result.status, RunStatus.COMPLETED)
        self.assertEqual(len(agent.session.run_records), 2)
        self.assertEqual(
            [event.kind for event in agent.session.events],
            [
                SessionEventType.CREATED,
                SessionEventType.STARTED,
                SessionEventType.INTERRUPTED,
                SessionEventType.RESUMED,
                SessionEventType.COMPLETED,
            ],
        )
        self.assertTrue(validate_tool_message_chain(agent.conversation.messages).ok)
        self.assertFalse(hasattr(agent, "tools_runner"))

    @patch("core.agent.LLMClient")
    def test_agent_starts_new_run_in_same_session_after_completed(
        self,
        llm_client_class,
    ):
        llm_client_class.return_value.chat.side_effect = (
            LLMResponse(type="text", content="第一轮完成"),
            LLMResponse(type="text", content="第二轮完成"),
        )
        agent = Agent(model="test-model")
        session = agent.session

        first_answer = agent.run("第一轮")
        second_answer = agent.run("第二轮")

        self.assertEqual(first_answer, "第一轮完成")
        self.assertEqual(second_answer, "第二轮完成")
        self.assertIs(agent.session, session)
        self.assertIs(agent.conversation, session.conversation)
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
        self.assertEqual(
            [
                message["content"]
                for message in session.conversation.messages
                if message["role"] == "user"
            ],
            ["第一轮", "第二轮"],
        )


if __name__ == "__main__":
    unittest.main()
