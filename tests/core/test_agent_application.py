import ast
import inspect
import unittest
from unittest.mock import Mock

from core.agent_application import AgentApplication
from core.session_runner import SessionRuntime
from core.tools_runtime.run_runtime import RunStatus


class AgentApplicationContractTest(unittest.TestCase):
    def setUp(self):
        self.session_runtime = Mock()
        self.application = AgentApplication(
            session_runtime=self.session_runtime,
            system_prompt="system prompt",
        )

    def test_constructor_rejects_empty_system_prompt(self):
        for system_prompt in ("", "   "):
            with self.subTest(system_prompt=system_prompt):
                with self.assertRaises(ValueError):
                    AgentApplication(
                        session_runtime=Mock(),
                        system_prompt=system_prompt,
                    )

    def test_application_does_not_hold_current_session_state(self):
        forbidden_attributes = {
            "session",
            "current_session",
            "conversation",
            "last_result",
        }

        self.assertTrue(
            forbidden_attributes.isdisjoint(vars(self.application)),
            vars(self.application),
        )

    def test_create_session_initializes_prompt_and_returns_only_session_id(self):
        self.session_runtime.create_session.return_value = Mock()

        result = self.application.create_session("session-1")

        self.assertEqual(result, "session-1")
        self.session_runtime.create_session.assert_called_once_with(
            session_id="session-1",
            system_prompt="system prompt",
        )

    def test_start_forwards_explicit_use_case_arguments_and_returns_outcome(self):
        outcome = object()
        self.session_runtime.start.return_value = outcome

        result = self.application.start(
            "session-1",
            "run-1",
            "开始任务",
            max_rounds=7,
        )

        self.assertIs(result, outcome)
        self.session_runtime.start.assert_called_once_with(
            "session-1",
            "run-1",
            "开始任务",
            7,
        )

    def test_resume_forwards_explicit_use_case_arguments_and_returns_outcome(self):
        outcome = object()
        self.session_runtime.resume.return_value = outcome

        result = self.application.resume(
            "session-1",
            "run-2",
            "继续任务",
            max_rounds=9,
        )

        self.assertIs(result, outcome)
        self.session_runtime.resume.assert_called_once_with(
            "session-1",
            "run-2",
            "继续任务",
            9,
        )

    def test_request_interrupt_forwards_session_id_and_reason(self):
        result = self.application.request_interrupt(
            "session-1",
            "console_interrupt",
        )

        self.assertIsNone(result)
        self.session_runtime.request_interrupt.assert_called_once_with(
            "session-1",
            "console_interrupt",
        )

    def test_session_runtime_errors_are_not_hidden(self):
        self.session_runtime.start.side_effect = KeyError("Session 不存在")

        with self.assertRaises(KeyError):
            self.application.start("missing", "run-1", "开始任务")

    def test_application_module_has_no_infrastructure_dependencies(self):
        source = inspect.getsource(inspect.getmodule(AgentApplication))
        tree = ast.parse(source)
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_modules.update(
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )

        forbidden_modules = {
            "json",
            "os",
            "pathlib",
            "core.model_call",
            "core.tools_runtime.run_runtime",
        }
        self.assertTrue(
            forbidden_modules.isdisjoint(imported_modules),
            imported_modules,
        )


class AgentApplicationSessionIsolationTest(unittest.TestCase):
    def test_one_application_operates_two_sessions_without_conversation_leak(self):
        def run(*, conversation, user_message, max_rounds, control):
            conversation.add_user(user_message)
            return Mock(status=RunStatus.COMPLETED, final_reason=None)

        run_runtime = Mock()
        run_runtime.run.side_effect = run
        session_runtime = SessionRuntime(run_runtime=run_runtime)
        application = AgentApplication(
            session_runtime=session_runtime,
            system_prompt="system prompt",
        )

        application.create_session("session-a")
        application.create_session("session-b")
        application.start("session-a", "run-a", "A 的消息")
        application.start("session-b", "run-b", "B 的消息")

        messages_a = session_runtime.sessions["session-a"].conversation.messages
        messages_b = session_runtime.sessions["session-b"].conversation.messages

        self.assertEqual(
            messages_a,
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "A 的消息"},
            ],
        )
        self.assertEqual(
            messages_b,
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "B 的消息"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
