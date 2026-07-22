import unittest
from unittest.mock import Mock, patch

from core.context import ContextState
from core.session_runner import MAX_USER_MESSAGE_CHARS, SessionRuntime
from core.session_state import (
    Session,
    SessionEvent,
    SessionEventType,
    SessionStatus,
)
from core.tools_runtime.run_runtime import RunStatus


class SessionRuntimeCreateSessionTest(unittest.TestCase):
    def setUp(self):
        self.runtime = SessionRuntime(run_runtime=Mock())

    def test_create_session_registers_pending_session_with_created_event(self):
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
        self.assertEqual(session.run_records, [])
        self.assertEqual(len(session.events), 1)
        self.assertEqual(session.events[0].kind, SessionEventType.CREATED)
        self.assertEqual(session.events[0].session_id, session.id)
        self.assertIsNone(session.events[0].run_id)

    def test_create_session_rejects_duplicate_id_without_replacing_original(self):
        original = self.runtime.create_session("session-1", system_prompt="prompt")

        with self.assertRaises(ValueError):
            self.runtime.create_session("session-1", system_prompt="prompt")

        self.assertIs(self.runtime.sessions["session-1"], original)
        self.assertEqual(len(self.runtime.sessions), 1)

    def test_create_session_requires_non_empty_id(self):
        for session_id in ("", "   "):
            with self.subTest(session_id=session_id):
                with self.assertRaises(ValueError):
                    self.runtime.create_session(session_id, system_prompt="prompt")

        self.assertEqual(self.runtime.sessions, {})

    def test_create_session_requires_non_empty_system_prompt(self):
        for system_prompt in ("", "   "):
            with self.subTest(system_prompt=system_prompt):
                with self.assertRaises(ValueError):
                    self.runtime.create_session(
                        "session-1",
                        system_prompt=system_prompt,
                    )

        self.assertEqual(self.runtime.sessions, {})

    def test_create_session_does_not_register_session_when_event_recording_fails(self):
        with patch.object(Session, "record_event", side_effect=ValueError("invalid event")):
            with self.assertRaises(ValueError):
                self.runtime.create_session("session-1", system_prompt="prompt")

        self.assertNotIn("session-1", self.runtime.sessions)


class SessionRuntimeStartTest(unittest.TestCase):
    def setUp(self):
        self.run_runtime = Mock()
        self.runtime = SessionRuntime(run_runtime=self.run_runtime)

    def test_start_exposes_control_during_run_and_cleans_it_afterwards(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")

        def run(*, conversation, user_message, max_rounds, control, context_state):
            self.assertIs(conversation, session.conversation)
            self.assertEqual(user_message, "完成任务")
            self.assertEqual(max_rounds, 20)
            self.assertIs(self.runtime.active_controls[session.id], control)
            self.assertEqual(session.status, SessionStatus.RUNNING)
            return Mock(
                status=RunStatus.COMPLETED,
                final_reason=None,
                context_state=context_state,
            )

        self.run_runtime.run.side_effect = run

        outcome = self.runtime.start("session-1", "run-1", "完成任务")
        record = outcome.record

        self.assertEqual(record.status, RunStatus.COMPLETED.value)
        self.assertEqual(outcome.result.status, RunStatus.COMPLETED)
        self.assertIsNotNone(record.ended_at)
        self.assertIsNone(record.final_reason)
        self.assertEqual(self.runtime.active_controls, {})

    def test_start_maps_each_run_status_to_session_status_and_event(self):
        cases = (
            (RunStatus.COMPLETED, SessionStatus.COMPLETED, SessionEventType.COMPLETED, None),
            (RunStatus.INTERRUPTED, SessionStatus.INTERRUPTED, SessionEventType.INTERRUPTED, "user_requested"),
            (RunStatus.BLOCKED, SessionStatus.BLOCKED, SessionEventType.BLOCKED, "budget_exhausted"),
            (RunStatus.FAILED, SessionStatus.FAILED, SessionEventType.FAILED, "llm_error"),
        )

        for index, (run_status, session_status, event_kind, reason) in enumerate(cases):
            with self.subTest(run_status=run_status):
                runtime = SessionRuntime(run_runtime=Mock())
                session = runtime.create_session(
                    f"session-{index}",
                    system_prompt="prompt",
                )
                runtime.run_runtime.run.return_value = Mock(
                    status=run_status,
                    final_reason=reason,
                    context_state=session.context_state,
                )

                outcome = runtime.start(session.id, f"run-{index}", "完成任务")
                record = outcome.record

                self.assertEqual(session.status, session_status)
                self.assertEqual(session.events[-1].kind, event_kind)
                self.assertEqual(session.events[-1].run_id, record.run_id)
                self.assertEqual(record.status, run_status.value)
                self.assertEqual(record.final_reason, reason)
                self.assertIsNotNone(record.ended_at)
                self.assertEqual(runtime.active_controls, {})

    def test_start_propagates_run_runtime_error_and_releases_control(self):
        self.runtime.create_session("session-1", system_prompt="prompt")
        self.run_runtime.run.side_effect = RuntimeError("runner crashed")

        with self.assertRaisesRegex(RuntimeError, "runner crashed"):
            self.runtime.start("session-1", "run-1", "完成任务")

        self.assertEqual(self.runtime.active_controls, {})

    def test_next_run_receives_context_state_committed_by_previous_run(self):
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
                status=RunStatus.COMPLETED,
                final_reason=None,
                context_state=returned_state,
            )

        self.run_runtime.run.side_effect = run

        self.runtime.start(session.id, "run-1", "第一轮")
        self.runtime.start(session.id, "run-2", "第二轮")

        self.assertEqual(seen_states, [ContextState(), advanced_state])
        self.assertIs(session.context_state, advanced_state)

    def test_start_rejects_oversized_user_message_without_entering_run(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")
        oversized = "x" * (MAX_USER_MESSAGE_CHARS + 1)
        message_count = len(session.conversation.records)

        with self.assertRaisesRegex(ValueError, "超过单次输入上限"):
            self.runtime.start(session.id, "run-1", oversized)

        self.run_runtime.run.assert_not_called()
        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.run_records, [])
        self.assertEqual(len(session.conversation.records), message_count)
        self.assertEqual(self.runtime.active_controls, {})


class SessionRuntimeRequestInterruptTest(unittest.TestCase):
    def setUp(self):
        self.run_runtime = Mock()
        self.runtime = SessionRuntime(run_runtime=self.run_runtime)

    def test_request_interrupt_marks_active_control_without_early_transition(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")

        def run(*, conversation, user_message, max_rounds, control, context_state):
            self.runtime.request_interrupt(session.id, "用户请求暂停")

            self.assertTrue(control.interrupt_requested)
            self.assertEqual(control.interrupt_reason, "用户请求暂停")
            self.assertEqual(max_rounds, 20)
            self.assertEqual(session.status, SessionStatus.RUNNING)
            return Mock(
                status=RunStatus.INTERRUPTED,
                final_reason="run_interrupted",
                context_state=context_state,
            )

        self.run_runtime.run.side_effect = run

        outcome = self.runtime.start(session.id, "run-1", "完成任务")
        record = outcome.record

        self.assertEqual(session.status, SessionStatus.INTERRUPTED)
        self.assertEqual(record.status, RunStatus.INTERRUPTED.value)
        self.assertEqual(self.runtime.active_controls, {})

    def test_request_interrupt_rejects_empty_or_unknown_session_id(self):
        for session_id in ("", "   "):
            with self.subTest(session_id=session_id):
                with self.assertRaises(ValueError):
                    self.runtime.request_interrupt(session_id)

        with self.assertRaises(KeyError):
            self.runtime.request_interrupt("missing")

    def test_request_interrupt_requires_running_session(self):
        self.runtime.create_session("session-1", system_prompt="prompt")

        with self.assertRaises(ValueError):
            self.runtime.request_interrupt("session-1")

    def test_request_interrupt_fails_when_running_session_has_no_control(self):
        session = self.runtime.create_session("session-1", system_prompt="prompt")
        session.transition_to(
            SessionStatus.RUNNING,
            SessionEvent(
                kind=SessionEventType.STARTED,
                session_id=session.id,
                reason="Session started",
                run_id="run-1",
            ),
        )

        with self.assertRaises(RuntimeError):
            self.runtime.request_interrupt(session.id)


class SessionRuntimeResumeTest(unittest.TestCase):
    def setUp(self):
        self.run_runtime = Mock()
        self.runtime = SessionRuntime(run_runtime=self.run_runtime)
        self.session = self.runtime.create_session(
            "session-1",
            system_prompt="prompt",
        )
        self.run_runtime.run.return_value = Mock(
            status=RunStatus.INTERRUPTED,
            final_reason="user_requested",
            context_state=self.session.context_state,
        )
        self.runtime.start(self.session.id, "run-1", "开始任务")
        self.run_runtime.reset_mock()

    def test_resume_starts_new_run_from_interrupted_session(self):
        def run(*, conversation, user_message, max_rounds, control, context_state):
            self.assertIs(conversation, self.session.conversation)
            self.assertEqual(user_message, "继续完成剩余任务")
            self.assertEqual(max_rounds, 20)
            self.assertIs(self.runtime.active_controls[self.session.id], control)
            self.assertEqual(self.session.status, SessionStatus.RUNNING)
            self.assertEqual(self.session.events[-1].kind, SessionEventType.RESUMED)
            self.assertEqual(self.session.events[-1].run_id, "run-2")
            return Mock(
                status=RunStatus.COMPLETED,
                final_reason=None,
                context_state=context_state,
            )

        self.run_runtime.run.side_effect = run

        outcome = self.runtime.resume(
            self.session.id,
            "run-2",
            "继续完成剩余任务",
        )
        record = outcome.record

        self.assertEqual(record.run_id, "run-2")
        self.assertEqual(record.status, RunStatus.COMPLETED.value)
        self.assertEqual(self.session.status, SessionStatus.COMPLETED)
        self.assertEqual(len(self.session.run_records), 2)
        self.assertEqual(self.runtime.active_controls, {})

    def test_resume_requires_non_empty_arguments(self):
        cases = (
            ("", "run-2", "继续"),
            ("   ", "run-2", "继续"),
            (self.session.id, "", "继续"),
            (self.session.id, "   ", "继续"),
            (self.session.id, "run-2", ""),
            (self.session.id, "run-2", "   "),
        )

        for session_id, run_id, user_message in cases:
            with self.subTest(
                session_id=session_id,
                run_id=run_id,
                user_message=user_message,
            ):
                with self.assertRaises(ValueError):
                    self.runtime.resume(session_id, run_id, user_message)

        self.run_runtime.run.assert_not_called()
        self.assertEqual(len(self.session.run_records), 1)
        self.assertEqual(self.session.status, SessionStatus.INTERRUPTED)

    def test_resume_rejects_oversized_user_message_without_entering_run(self):
        oversized = "x" * (MAX_USER_MESSAGE_CHARS + 1)
        message_count = len(self.session.conversation.records)

        with self.assertRaisesRegex(ValueError, "超过单次输入上限"):
            self.runtime.resume(self.session.id, "run-2", oversized)

        self.run_runtime.run.assert_not_called()
        self.assertEqual(self.session.status, SessionStatus.INTERRUPTED)
        self.assertEqual(len(self.session.run_records), 1)
        self.assertEqual(len(self.session.conversation.records), message_count)
        self.assertEqual(self.runtime.active_controls, {})

    def test_resume_rejects_unknown_session(self):
        with self.assertRaises(KeyError):
            self.runtime.resume("missing", "run-2", "继续")

        self.run_runtime.run.assert_not_called()

    def test_resume_requires_interrupted_session(self):
        pending = self.runtime.create_session("session-2", system_prompt="prompt")

        with self.assertRaises(ValueError):
            self.runtime.resume(pending.id, "run-2", "继续")

        self.run_runtime.run.assert_not_called()
        self.assertEqual(pending.status, SessionStatus.PENDING)
        self.assertEqual(pending.run_records, [])

    def test_resume_rejects_duplicate_run_id(self):
        with self.assertRaises(ValueError):
            self.runtime.resume(self.session.id, "run-1", "继续")

        self.run_runtime.run.assert_not_called()
        self.assertEqual(len(self.session.run_records), 1)
        self.assertEqual(self.session.status, SessionStatus.INTERRUPTED)

    def test_resume_rejects_existing_active_control(self):
        self.runtime.active_controls[self.session.id] = Mock()

        with self.assertRaises(ValueError):
            self.runtime.resume(self.session.id, "run-2", "继续")

        self.run_runtime.run.assert_not_called()
        self.assertEqual(len(self.session.run_records), 1)
        self.assertEqual(self.session.status, SessionStatus.INTERRUPTED)

    def test_resume_propagates_run_runtime_error_and_releases_control(self):
        self.run_runtime.run.side_effect = RuntimeError("runner crashed")

        with self.assertRaisesRegex(RuntimeError, "runner crashed"):
            self.runtime.resume(self.session.id, "run-2", "继续")

        self.assertEqual(self.runtime.active_controls, {})


if __name__ == "__main__":
    unittest.main()
