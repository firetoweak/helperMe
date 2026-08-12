import ast
import inspect
import unittest
from unittest.mock import AsyncMock, Mock

import tools  # noqa: F401
from core.agent_application import AgentApplication
from core.model_call import LLMResponse, ToolCall
from core.observability import format_run_log
from core.runtime_modes import PlainMode
from core.session import SessionRuntime
from core.session.state import SessionEventType, SessionStatus
from core.tools_runtime.run_runtime import RunRuntime, RunStatus
from core.tools_runtime.run_invocation import RunInvocation
from core.tools_runtime.tools_protocol import validate_tool_message_chain
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
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


class AgentApplicationContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session_runtime = Mock()
        self.session_runtime.active_controls = {}
        self.session_runtime.start = AsyncMock()
        self.session_runtime.resume = AsyncMock()
        self.application = AgentApplication(
            session_runtime=self.session_runtime,
            system_prompt="system prompt",
        )
        await self.application.__aenter__()

    async def asyncTearDown(self):
        await self.application.close()

    async def test_constructor_rejects_empty_system_prompt(self):
        for system_prompt in ("", "   "):
            with self.subTest(system_prompt=system_prompt):
                with self.assertRaises(ValueError):
                    AgentApplication(
                        session_runtime=Mock(),
                        system_prompt=system_prompt,
                    )

    async def test_application_does_not_hold_current_session_state(self):
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

    async def test_create_session_initializes_prompt_and_returns_only_session_id(self):
        self.session_runtime.create_session.return_value = Mock()

        result = self.application.create_session("session-1")

        self.assertEqual(result, "session-1")
        self.session_runtime.create_session.assert_called_once_with(
            session_id="session-1",
            system_prompt="system prompt",
        )

    async def test_start_forwards_explicit_use_case_arguments_and_returns_outcome(self):
        outcome = object()
        self.session_runtime.start.return_value = outcome

        result = await self.application.start(
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

    async def test_resume_forwards_explicit_use_case_arguments_and_returns_outcome(self):
        outcome = object()
        self.session_runtime.resume.return_value = outcome

        result = await self.application.resume(
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

    async def test_application_owns_the_default_run_round_limit(self):
        application = AgentApplication(
            self.session_runtime,
            "system prompt",
            default_max_rounds=73,
        )
        outcome = object()
        self.session_runtime.start.return_value = outcome

        async with application:
            result = await application.start("session-1", "run-1", "开始任务")

        self.assertIs(result, outcome)
        self.session_runtime.start.assert_called_once_with(
            "session-1",
            "run-1",
            "开始任务",
            73,
        )

    async def test_application_rejects_invalid_default_run_round_limit(self):
        with self.assertRaisesRegex(ValueError, "必须大于 0"):
            AgentApplication(
                self.session_runtime,
                "system prompt",
                default_max_rounds=0,
            )

    async def test_run_host_validates_before_plugin_domain_mutation(self):
        session = Mock(status=SessionStatus.COMPLETED, run_records=[])
        self.session_runtime.get_session.return_value = session

        self.application.validate_run("session-1", "run-1", "执行任务")

        self.session_runtime.validate_run_input.assert_called_once_with(
            "run-1",
            "执行任务",
        )
        self.session_runtime.get_session.assert_called_once_with("session-1")

    async def test_run_host_selects_resume_for_interrupted_session(self):
        invocation = RunInvocation()
        self.session_runtime.get_session.return_value = Mock(
            status=SessionStatus.INTERRUPTED,
        )
        outcome = object()
        self.session_runtime.resume.return_value = outcome

        result = await self.application.execute(
            "session-1",
            "run-2",
            "继续任务",
            9,
            invocation,
        )

        self.assertIs(result, outcome)
        self.session_runtime.resume.assert_called_once_with(
            "session-1",
            "run-2",
            "继续任务",
            9,
            invocation=invocation,
        )

    async def test_request_interrupt_forwards_session_id_and_reason(self):
        result = self.application.request_interrupt(
            "session-1",
            "console_interrupt",
        )

        self.assertIsNone(result)
        self.session_runtime.request_interrupt.assert_called_once_with(
            "session-1",
            "console_interrupt",
        )

    async def test_delete_session_forwards_explicit_use_case(self):
        result = self.application.delete_session("session-1")

        self.assertIsNone(result)
        self.session_runtime.delete_session.assert_called_once_with("session-1")

    async def test_session_runtime_errors_are_not_hidden(self):
        self.session_runtime.start.side_effect = KeyError("Session 不存在")

        with self.assertRaises(KeyError):
            await self.application.start("missing", "run-1", "开始任务")

    async def test_application_module_has_no_infrastructure_dependencies(self):
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


class RecordingApplicationResource:
    def __init__(self, name, events, enter_error=None):
        self.name = name
        self.events = events
        self.enter_error = enter_error

    async def __aenter__(self):
        self.events.append(f"enter:{self.name}")
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append(f"exit:{self.name}")


class AgentApplicationResourceLifecycleTest(
    unittest.IsolatedAsyncioTestCase
):
    async def test_resources_are_closed_in_reverse_order(self):
        events = []
        application = AgentApplication(
            Mock(active_controls={}),
            "system prompt",
            resources=(
                RecordingApplicationResource("first", events),
                RecordingApplicationResource("second", events),
            ),
        )

        async with application:
            self.assertEqual(events, ["enter:first", "enter:second"])

        self.assertEqual(
            events,
            ["enter:first", "enter:second", "exit:second", "exit:first"],
        )

    async def test_partial_enter_failure_closes_entered_resources(self):
        events = []
        error = RuntimeError("resource failed")
        application = AgentApplication(
            Mock(active_controls={}),
            "system prompt",
            resources=(
                RecordingApplicationResource("first", events),
                RecordingApplicationResource("second", events, error),
            ),
        )

        with self.assertRaises(RuntimeError) as captured:
            async with application:
                self.fail("resource failure must prevent application use")

        self.assertIs(captured.exception, error)
        self.assertEqual(events, ["enter:first", "enter:second", "exit:first"])

    async def test_close_does_not_hide_body_exception(self):
        events = []
        error = RuntimeError("run failed")
        application = AgentApplication(
            Mock(active_controls={}),
            "system prompt",
            resources=(RecordingApplicationResource("mcp", events),),
        )

        with self.assertRaises(RuntimeError) as captured:
            async with application:
                raise error

        self.assertIs(captured.exception, error)
        self.assertEqual(events, ["enter:mcp", "exit:mcp"])

    async def test_business_calls_require_active_lifecycle(self):
        runtime = Mock(active_controls={})
        application = AgentApplication(runtime, "system prompt")

        with self.assertRaisesRegex(RuntimeError, "async with"):
            application.create_session("session-1")

        await application.__aenter__()
        await application.close()

        with self.assertRaisesRegex(RuntimeError, "closed"):
            application.create_session("session-1")

    async def test_application_cannot_be_entered_twice(self):
        application = AgentApplication(
            Mock(active_controls={}),
            "system prompt",
        )
        await application.__aenter__()
        self.addAsyncCleanup(application.close)

        with self.assertRaisesRegex(RuntimeError, "不能进入"):
            await application.__aenter__()

    async def test_close_rejects_active_run_without_closing_resources(self):
        events = []
        runtime = Mock(active_controls={})
        application = AgentApplication(
            runtime,
            "system prompt",
            resources=(RecordingApplicationResource("mcp", events),),
        )
        await application.__aenter__()
        runtime.active_controls["session-1"] = Mock()

        with self.assertRaisesRegex(RuntimeError, "活动 Run"):
            await application.close()

        self.assertEqual(events, ["enter:mcp"])
        runtime.active_controls.clear()
        await application.close()
        self.assertEqual(events, ["enter:mcp", "exit:mcp"])


class AgentApplicationSessionIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_application_operates_two_sessions_without_conversation_leak(self):
        async def run(*, conversation, user_message, max_rounds, control, context_state):
            conversation.add_user(user_message)
            return Mock(
                status=RunStatus.COMPLETED,
                final_reason=None,
                context_state=context_state,
            )

        run_runtime = Mock()
        run_runtime.run = AsyncMock()
        run_runtime.run.side_effect = run
        session_runtime = SessionRuntime(run_runtime=run_runtime)
        application = AgentApplication(
            session_runtime=session_runtime,
            system_prompt="system prompt",
        )

        async with application:
            application.create_session("session-a")
            application.create_session("session-b")
            await application.start("session-a", "run-a", "A 的消息")
            await application.start("session-b", "run-b", "B 的消息")

        messages_a = (
            session_runtime.sessions["session-a"]
            .conversation.protocol_messages()
        )
        messages_b = (
            session_runtime.sessions["session-b"]
            .conversation.protocol_messages()
        )

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


class AgentApplicationSessionRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def _build_application(self, llm_client: Mock):
        session_runtime = SessionRuntime(
            RunRuntime(
                model_calls=model_call_service(llm_client),
                model="test-model",
                runtime_mode=PlainMode(),
                context_preparation=context_preparation_service(),
                **runtime_tool_dependencies(SUCCESS),
            )
        )
        application = AgentApplication(
            session_runtime=session_runtime,
            system_prompt="system prompt",
        )
        await application.__aenter__()
        self.addAsyncCleanup(application.close)
        application.create_session("session-1")
        return application, session_runtime

    async def test_application_starts_and_resumes_through_session_runtime(
        self,
    ):
        llm_client = Mock()
        llm_client.chat = AsyncMock()
        responses = iter(
            (
                LLMResponse(
                    calls=(ToolCall("call-1", "demo", "{}"),),
                ),
                LLMResponse(content="任务已完成"),
            )
        )
        application = None

        async def chat(messages, model, tools=None):
            response = next(responses)
            if response.calls:
                application.request_interrupt("session-1", "等待继续")
            return call_result(response)

        llm_client.chat.side_effect = chat
        application, session_runtime = await self._build_application(llm_client)
        session = session_runtime.sessions["session-1"]

        interrupted = await application.start("session-1", "run-1", "开始任务")

        self.assertEqual(interrupted.result.status, RunStatus.INTERRUPTED)
        self.assertEqual(session.status, SessionStatus.INTERRUPTED)
        self.assertTrue(
            validate_tool_message_chain(
                session.conversation.protocol_messages()
            ).ok
        )

        completed = await application.resume("session-1", "run-2", "继续执行")

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
        self.assertTrue(
            validate_tool_message_chain(
                session.conversation.protocol_messages()
            ).ok
        )

    async def test_application_starts_new_run_in_same_session_after_completed(self):
        llm_client = Mock()
        llm_client.chat = AsyncMock()
        llm_client.chat.side_effect = (
            call_result(LLMResponse(content="第一轮完成")),
            call_result(LLMResponse(content="第二轮完成")),
        )
        application, session_runtime = await self._build_application(llm_client)
        session = session_runtime.sessions["session-1"]

        first = await application.start("session-1", "run-1", "第一轮")
        second = await application.start("session-1", "run-2", "第二轮")

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


class ObservabilityContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_todo_revision_checkpoint_is_written_to_run_log(self):
        trace = {
            "started_at": "2026-01-01 00:00:00",
            "ended_at": "2026-01-01 00:00:01",
            "model": "test-model",
            "system_prompt": "system prompt",
            "model_requests": [
                {
                    "stage": "agent_round",
                    "round_index": 1,
                    "attempt": 1,
                    "runtime_prompts": ["runtime prompt"],
                    "messages": [
                        {"role": "system", "content": "system prompt"},
                        {"role": "user", "content": "hello"},
                    ],
                }
            ],
            "run_id": "run-1",
            "status": "completed",
            "question": "hello",
            "answer": "world",
            "checkpoints": [
                {
                    "kind": "tool_batch",
                    "reason": "tool_batch_completed",
                    "message": "TodoList 已同步。",
                    "data": {
                        "todo_list": {
                            "phase": "active",
                            "sync_state": "clean",
                            "revision": 2,
                        },
                    },
                }
            ],
        }

        log = format_run_log(trace)

        self.assertIn('"reason": "tool_batch_completed"', log)
        self.assertIn('"todo_list": {', log)
        self.assertIn('"sync_state": "clean"', log)
        self.assertIn('"revision": 2', log)
        self.assertIn("System Prompt:\nsystem prompt", log)
        self.assertIn('"runtime_prompts": [', log)
        self.assertIn('"content": "hello"', log)

    async def test_missing_internal_trace_field_is_not_defaulted(self):
        trace = {
            "started_at": "2026-01-01 00:00:00",
            "ended_at": "2026-01-01 00:00:01",
            "model": "test-model",
            "run_id": "run-1",
            "status": "completed",
            "question": "hello",
            "answer": "world",
        }

        with self.assertRaises(KeyError):
            format_run_log(trace)


if __name__ == "__main__":
    unittest.main()
