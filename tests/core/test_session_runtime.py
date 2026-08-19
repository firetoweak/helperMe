import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.context import ContextState
from core.session import MAX_USER_MESSAGE_CHARS, SessionRuntime
from core.session.state import (
    Session,
    SessionEvent,
    SessionEventType,
    SessionStatus,
)
from core.tools_runtime.turn_runtime import TurnStatus


class SessionRuntimeCreateSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = SessionRuntime(turn_runtime=Mock())

    async def test_create_session_registers_pending_session_with_created_event(self):
        session = self.runtime.create_session(
            "session-1",
            system_prompt="system prompt",
        )

        self.assertIs(self.runtime.sessions["session-1"], session)
        self.assertEqual(
            session.conversation.protocol_messages(),
            [{"role": "system", "content": "system prompt"}],
        )
        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.turn_records, [])
        self.assertEqual(len(session.events), 1)
        self.assertEqual(session.events[0].kind, SessionEventType.CREATED)
        self.assertEqual(session.events[0].session_id, session.id)
        self.assertIsNone(session.events[0].turn_id)

    async def test_create_session_rejects_duplicate_id_without_replacing_original(self):
        original = self.runtime.create_session("session-1", system_prompt="prompt")

        with self.assertRaises(ValueError):
            self.runtime.create_session("session-1", system_prompt="prompt")

        self.assertIs(self.runtime.sessions["session-1"], original)
        self.assertEqual(len(self.runtime.sessions), 1)

    async def test_create_session_requires_non_empty_id(self):
        for session_id in ("", "   "):
            with self.subTest(session_id=session_id):
                with self.assertRaises(ValueError):
                    self.runtime.create_session(session_id, system_prompt="prompt")

        self.assertEqual(self.runtime.sessions, {})

    async def test_create_session_requires_non_empty_system_prompt(self):
        for system_prompt in ("", "   "):
            with self.subTest(system_prompt=system_prompt):
                with self.assertRaises(ValueError):
                    self.runtime.create_session(
                        "session-1",
                        system_prompt=system_prompt,
                    )

        self.assertEqual(self.runtime.sessions, {})

    async def test_create_session_does_not_register_session_when_event_recording_fails(self):
        with patch.object(Session, "record_event", side_effect=ValueError("invalid event")):
            with self.assertRaises(ValueError):
                self.runtime.create_session("session-1", system_prompt="prompt")

        self.assertNotIn("session-1", self.runtime.sessions)


class SessionRuntimeDeleteSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.created_runtimes = {}
        self.deleted_sessions = []

        def create_runtime(session_id):
            runtime = Mock()
            runtime.run = AsyncMock()
            self.created_runtimes[session_id] = runtime
            return runtime

        self.runtime = SessionRuntime(
            turn_runtime_factory=create_runtime,
            delete_session_resources=self.deleted_sessions.append,
        )

    async def test_delete_session_removes_state_runtime_and_resources(self):
        self.runtime.create_session("session-1", system_prompt="prompt")

        self.runtime.delete_session("session-1")

        self.assertNotIn("session-1", self.runtime.sessions)
        self.assertNotIn("session-1", self.runtime._session_turn_runtimes)
        self.assertEqual(self.deleted_sessions, ["session-1"])

    async def test_delete_session_rejects_active_session_without_deleting_resources(self):
        self.runtime.create_session("session-1", system_prompt="prompt")
        self.runtime.active_controls["session-1"] = Mock()

        with self.assertRaisesRegex(ValueError, "正在执行"):
            self.runtime.delete_session("session-1")

        self.assertIn("session-1", self.runtime.sessions)
        self.assertEqual(self.deleted_sessions, [])

    async def test_resource_delete_failure_preserves_session_state(self):
        runtime = SessionRuntime(
            turn_runtime_factory=lambda _session_id: Mock(),
            delete_session_resources=Mock(
                side_effect=OSError("drawer delete failed")
            ),
        )
        runtime.create_session("session-1", system_prompt="prompt")

        with self.assertRaisesRegex(OSError, "drawer delete failed"):
            runtime.delete_session("session-1")

        self.assertIn("session-1", runtime.sessions)
        self.assertIn("session-1", runtime._session_turn_runtimes)

    async def test_completed_turn_does_not_delete_session_drawer(self):
        session = self.runtime.create_session(
            "session-1",
            system_prompt="prompt",
        )
        self.created_runtimes[session.id].run.return_value = Mock(
            status=TurnStatus.COMPLETED,
            final_reason=None,
            context_state=session.context_state,
        )

        await self.runtime.start(session.id, "turn-1", "完成任务")

        self.assertIn(session.id, self.runtime.sessions)
        self.assertIn(session.id, self.runtime._session_turn_runtimes)
        self.assertEqual(self.deleted_sessions, [])


class SessionRuntimeStartTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turn_runtime = Mock()
        self.turn_runtime.run = AsyncMock()
        self.runtime = SessionRuntime(turn_runtime=self.turn_runtime)

    async def test_start_exposes_control_during_turn_and_cleans_it_afterwards(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")

        def run(*, conversation, user_message, max_steps, control, context_state):
            self.assertIs(conversation, session.conversation)
            self.assertEqual(user_message, "完成任务")
            self.assertEqual(max_steps, 20)
            self.assertIs(self.runtime.active_controls[session.id], control)
            self.assertEqual(session.status, SessionStatus.RUNNING)
            return Mock(
                status=TurnStatus.COMPLETED,
                final_reason=None,
                context_state=context_state,
            )

        self.turn_runtime.run.side_effect = run

        outcome = await self.runtime.start("session-1", "turn-1", "完成任务")
        record = outcome.record

        self.assertEqual(record.status, TurnStatus.COMPLETED.value)
        self.assertEqual(outcome.result.status, TurnStatus.COMPLETED)
        self.assertIsNotNone(record.ended_at)
        self.assertIsNone(record.final_reason)
        self.assertEqual(self.runtime.active_controls, {})

    async def test_start_maps_each_turn_status_to_session_status_and_event(self):
        cases = (
            (TurnStatus.COMPLETED, SessionStatus.COMPLETED, SessionEventType.COMPLETED, None),
            (TurnStatus.INTERRUPTED, SessionStatus.INTERRUPTED, SessionEventType.INTERRUPTED, "user_requested"),
            (TurnStatus.BLOCKED, SessionStatus.BLOCKED, SessionEventType.BLOCKED, "budget_exhausted"),
            (TurnStatus.FAILED, SessionStatus.FAILED, SessionEventType.FAILED, "llm_error"),
        )

        for index, (turn_status, session_status, event_kind, reason) in enumerate(cases):
            with self.subTest(turn_status=turn_status):
                turn_runtime = Mock()
                turn_runtime.run = AsyncMock()
                runtime = SessionRuntime(turn_runtime=turn_runtime)
                session = runtime.create_session(
                    f"session-{index}",
                    system_prompt="prompt",
                )
                runtime.turn_runtime.run.return_value = Mock(
                    status=turn_status,
                    final_reason=reason,
                    context_state=session.context_state,
                )

                outcome = await runtime.start(session.id, f"turn-{index}", "完成任务")
                record = outcome.record

                self.assertEqual(session.status, session_status)
                self.assertEqual(session.events[-1].kind, event_kind)
                self.assertEqual(session.events[-1].turn_id, record.turn_id)
                self.assertEqual(record.status, turn_status.value)
                self.assertEqual(record.final_reason, reason)
                self.assertIsNotNone(record.ended_at)
                self.assertEqual(runtime.active_controls, {})

    async def test_start_propagates_turn_runtime_error_and_releases_control(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")
        self.turn_runtime.run.side_effect = RuntimeError("runner crashed")

        with self.assertRaisesRegex(RuntimeError, "runner crashed"):
            await self.runtime.start("session-1", "turn-1", "完成任务")

        self.assertEqual(self.runtime.active_controls, {})
        self.assertEqual(session.status, SessionStatus.FAILED)
        self.assertEqual(session.turn_records[0].status, TurnStatus.FAILED.value)
        self.assertEqual(session.turn_records[0].final_reason, "runtime_exception")
        self.assertIsNotNone(session.turn_records[0].ended_at)

    async def test_cancelled_turn_is_finalized_before_cancellation_propagates(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")
        entered = asyncio.Event()

        async def run(**_kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.turn_runtime.run.side_effect = run
        task = asyncio.create_task(
            self.runtime.start("session-1", "turn-1", "完成任务")
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        record = session.turn_records[0]
        self.assertEqual(session.status, SessionStatus.FAILED)
        self.assertEqual(session.events[-1].kind, SessionEventType.FAILED)
        self.assertEqual(record.status, TurnStatus.FAILED.value)
        self.assertEqual(record.final_reason, "task_cancelled")
        self.assertIsNotNone(record.ended_at)
        self.assertEqual(self.runtime.active_controls, {})

    async def test_next_turn_receives_context_state_committed_by_previous_turn(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")
        advanced_state = ContextState(
            tool_artifacts={
                session.conversation.records[0].message_id: "art_" + "9" * 32
            }
        )
        seen_states = []

        def run(**kwargs):
            seen_states.append(kwargs["context_state"])
            returned_state = advanced_state if len(seen_states) == 1 else kwargs["context_state"]
            return Mock(
                status=TurnStatus.COMPLETED,
                final_reason=None,
                context_state=returned_state,
            )

        self.turn_runtime.run.side_effect = run

        await self.runtime.start(session.id, "turn-1", "第一轮")
        await self.runtime.start(session.id, "turn-2", "第二轮")

        self.assertEqual(seen_states, [ContextState(), advanced_state])
        self.assertIs(session.context_state, advanced_state)

    async def test_start_rejects_oversized_user_message_without_entering_turn(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")
        oversized = "x" * (MAX_USER_MESSAGE_CHARS + 1)
        message_count = len(session.conversation.records)

        with self.assertRaisesRegex(ValueError, "超过单次输入上限"):
            await self.runtime.start(session.id, "turn-1", oversized)

        self.turn_runtime.run.assert_not_called()
        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.turn_records, [])
        self.assertEqual(len(session.conversation.records), message_count)
        self.assertEqual(self.runtime.active_controls, {})


class SessionRuntimeRequestInterruptTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turn_runtime = Mock()
        self.turn_runtime.run = AsyncMock()
        self.runtime = SessionRuntime(turn_runtime=self.turn_runtime)

    async def test_request_interrupt_marks_active_control_without_early_transition(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")

        def run(*, conversation, user_message, max_steps, control, context_state):
            self.runtime.request_interrupt(session.id, "用户请求暂停")

            self.assertTrue(control.interrupt_requested)
            self.assertEqual(control.interrupt_reason, "用户请求暂停")
            self.assertEqual(max_steps, 20)
            self.assertEqual(session.status, SessionStatus.RUNNING)
            return Mock(
                status=TurnStatus.INTERRUPTED,
                final_reason="turn_interrupted",
                context_state=context_state,
            )

        self.turn_runtime.run.side_effect = run

        outcome = await self.runtime.start(session.id, "turn-1", "完成任务")
        record = outcome.record

        self.assertEqual(session.status, SessionStatus.INTERRUPTED)
        self.assertEqual(record.status, TurnStatus.INTERRUPTED.value)
        self.assertEqual(self.runtime.active_controls, {})

    async def test_request_interrupt_rejects_empty_or_unknown_session_id(self):
        for session_id in ("", "   "):
            with self.subTest(session_id=session_id):
                with self.assertRaises(ValueError):
                    self.runtime.request_interrupt(session_id)

        with self.assertRaises(KeyError):
            self.runtime.request_interrupt("missing")

    async def test_request_interrupt_requires_running_session(self):
        self.runtime.create_session("session-1", system_prompt="prompt")

        with self.assertRaises(ValueError):
            self.runtime.request_interrupt("session-1")

    async def test_request_interrupt_fails_when_running_session_has_no_control(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")
        session.transition_to(
            SessionStatus.RUNNING,
            SessionEvent(
                kind=SessionEventType.STARTED,
                session_id=session.id,
                reason="Session started",
                turn_id="turn-1",
            ),
        )

        with self.assertRaises(RuntimeError):
            self.runtime.request_interrupt(session.id)


class SessionRuntimeResumeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turn_runtime = Mock()
        self.turn_runtime.run = AsyncMock()
        self.runtime = SessionRuntime(turn_runtime=self.turn_runtime)
        self.session = self.runtime.create_session(
            "session-1",
            system_prompt="prompt",
        )
        self.turn_runtime.run.return_value = Mock(
            status=TurnStatus.INTERRUPTED,
            final_reason="user_requested",
            context_state=self.session.context_state,
        )
    async def asyncSetUp(self):
        await self.runtime.start(self.session.id, "turn-1", "开始任务")
        self.turn_runtime.reset_mock()
        self.turn_runtime.reset_mock()

    async def test_resume_starts_new_turn_from_interrupted_session(self):
        def run(*, conversation, user_message, max_steps, control, context_state):
            self.assertIs(conversation, self.session.conversation)
            self.assertEqual(user_message, "继续完成剩余任务")
            self.assertEqual(max_steps, 20)
            self.assertIs(self.runtime.active_controls[self.session.id], control)
            self.assertEqual(self.session.status, SessionStatus.RUNNING)
            self.assertEqual(self.session.events[-1].kind, SessionEventType.RESUMED)
            self.assertEqual(self.session.events[-1].turn_id, "turn-2")
            return Mock(
                status=TurnStatus.COMPLETED,
                final_reason=None,
                context_state=context_state,
            )

        self.turn_runtime.run.side_effect = run

        outcome = await self.runtime.resume(
            self.session.id,
            "turn-2",
            "继续完成剩余任务",
        )
        record = outcome.record

        self.assertEqual(record.turn_id, "turn-2")
        self.assertEqual(record.status, TurnStatus.COMPLETED.value)
        self.assertEqual(self.session.status, SessionStatus.COMPLETED)
        self.assertEqual(len(self.session.turn_records), 2)
        self.assertEqual(self.runtime.active_controls, {})

    async def test_resume_requires_non_empty_arguments(self):
        cases = (
            ("", "turn-2", "继续"),
            ("   ", "turn-2", "继续"),
            (self.session.id, "", "继续"),
            (self.session.id, "   ", "继续"),
            (self.session.id, "turn-2", ""),
            (self.session.id, "turn-2", "   "),
        )

        for session_id, turn_id, user_message in cases:
            with self.subTest(
                session_id=session_id,
                turn_id=turn_id,
                user_message=user_message,
            ):
                with self.assertRaises(ValueError):
                    await self.runtime.resume(session_id, turn_id, user_message)

        self.turn_runtime.run.assert_not_called()
        self.assertEqual(len(self.session.turn_records), 1)
        self.assertEqual(self.session.status, SessionStatus.INTERRUPTED)

    async def test_resume_rejects_oversized_user_message_without_entering_turn(self):
        oversized = "x" * (MAX_USER_MESSAGE_CHARS + 1)
        message_count = len(self.session.conversation.records)

        with self.assertRaisesRegex(ValueError, "超过单次输入上限"):
            await self.runtime.resume(self.session.id, "turn-2", oversized)

        self.turn_runtime.run.assert_not_called()
        self.assertEqual(self.session.status, SessionStatus.INTERRUPTED)
        self.assertEqual(len(self.session.turn_records), 1)
        self.assertEqual(len(self.session.conversation.records), message_count)
        self.assertEqual(self.runtime.active_controls, {})

    async def test_resume_rejects_unknown_session(self):
        with self.assertRaises(KeyError):
            await self.runtime.resume("missing", "turn-2", "继续")

        self.turn_runtime.run.assert_not_called()

    async def test_resume_requires_interrupted_session(self):
        pending = self.runtime.create_session("session-2", system_prompt="prompt")

        with self.assertRaises(ValueError):
            await self.runtime.resume(pending.id, "turn-2", "继续")

        self.turn_runtime.run.assert_not_called()
        self.assertEqual(pending.status, SessionStatus.PENDING)
        self.assertEqual(pending.turn_records, [])

    async def test_resume_rejects_duplicate_turn_id(self):
        with self.assertRaises(ValueError):
            await self.runtime.resume(self.session.id, "turn-1", "继续")

        self.turn_runtime.run.assert_not_called()
        self.assertEqual(len(self.session.turn_records), 1)
        self.assertEqual(self.session.status, SessionStatus.INTERRUPTED)

    async def test_resume_rejects_existing_active_control(self):
        self.runtime.active_controls[self.session.id] = Mock()

        with self.assertRaises(ValueError):
            await self.runtime.resume(self.session.id, "turn-2", "继续")

        self.turn_runtime.run.assert_not_called()
        self.assertEqual(len(self.session.turn_records), 1)
        self.assertEqual(self.session.status, SessionStatus.INTERRUPTED)

    async def test_resume_propagates_turn_runtime_error_and_releases_control(self):
        self.turn_runtime.run.side_effect = RuntimeError("runner crashed")

        with self.assertRaisesRegex(RuntimeError, "runner crashed"):
            await self.runtime.resume(self.session.id, "turn-2", "继续")

        self.assertEqual(self.runtime.active_controls, {})
        self.assertEqual(self.session.status, SessionStatus.FAILED)
        self.assertEqual(
            self.session.turn_records[-1].final_reason,
            "runtime_exception",
        )


if __name__ == "__main__":
    unittest.main()
