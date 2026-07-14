import unittest
from datetime import datetime, timezone

from core.session_state import (
    InvalidSessionTransition,
    Session,
    SessionEvent,
    SessionEventType,
    SessionRunRecord,
    SessionStatus,
)


class SessionStateTest(unittest.TestCase):
    def make_event(
        self,
        kind: SessionEventType,
        *,
        session_id: str = "session-1",
        run_id: str = "run-1",
    ) -> SessionEvent:
        return SessionEvent(
            kind=kind,
            session_id=session_id,
            reason="test transition",
            run_id=run_id,
        )

    def test_new_session_is_pending(self):
        session = Session(id="session-1")

        self.assertEqual(session.status, SessionStatus.PENDING)

    def test_sessions_do_not_share_conversation(self):
        first = Session(id="session-1")
        second = Session(id="session-2")

        self.assertIsNot(first.conversation, second.conversation)

    def test_interrupt_and_resume(self):
        session = Session(id="session-1")

        started = self.make_event(SessionEventType.STARTED, run_id="run-1")
        interrupted = self.make_event(
            SessionEventType.INTERRUPTED,
            run_id="run-1",
        )
        resumed = self.make_event(SessionEventType.RESUMED, run_id="run-2")

        session.transition_to(SessionStatus.RUNNING, started)
        session.transition_to(SessionStatus.INTERRUPTED, interrupted)
        session.transition_to(SessionStatus.RUNNING, resumed)

        self.assertEqual(session.status, SessionStatus.RUNNING)
        self.assertEqual(session.events, [started, interrupted, resumed])

    def test_pending_cannot_be_interrupted(self):
        session = Session(id="session-1")
        event = self.make_event(SessionEventType.INTERRUPTED)

        with self.assertRaises(InvalidSessionTransition):
            session.transition_to(SessionStatus.INTERRUPTED, event)

        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.events, [])

    def test_transition_rejects_event_from_another_session_atomically(self):
        session = Session(id="session-1")
        event = self.make_event(
            SessionEventType.STARTED,
            session_id="session-2",
        )

        with self.assertRaises(ValueError):
            session.transition_to(SessionStatus.RUNNING, event)

        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.events, [])

    def test_transition_rejects_event_kind_mismatched_with_target_atomically(self):
        session = Session(id="session-1")
        event = self.make_event(SessionEventType.COMPLETED)

        with self.assertRaises(ValueError):
            session.transition_to(SessionStatus.RUNNING, event)

        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.events, [])

    def test_pending_to_running_requires_started_event(self):
        session = Session(id="session-1")
        event = self.make_event(SessionEventType.RESUMED)

        with self.assertRaises(ValueError):
            session.transition_to(SessionStatus.RUNNING, event)

        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.events, [])

    def test_interrupted_to_running_requires_resumed_event(self):
        session = Session(id="session-1")
        session.transition_to(
            SessionStatus.RUNNING,
            self.make_event(SessionEventType.STARTED),
        )
        session.transition_to(
            SessionStatus.INTERRUPTED,
            self.make_event(SessionEventType.INTERRUPTED),
        )
        event = self.make_event(SessionEventType.STARTED, run_id="run-2")

        with self.assertRaises(ValueError):
            session.transition_to(SessionStatus.RUNNING, event)

        self.assertEqual(session.status, SessionStatus.INTERRUPTED)
        self.assertNotIn(event, session.events)

    def test_running_can_transition_to_each_terminal_status_with_matching_event(self):
        cases = (
            (SessionStatus.INTERRUPTED, SessionEventType.INTERRUPTED),
            (SessionStatus.COMPLETED, SessionEventType.COMPLETED),
            (SessionStatus.BLOCKED, SessionEventType.BLOCKED),
            (SessionStatus.FAILED, SessionEventType.FAILED),
        )

        for target, event_kind in cases:
            with self.subTest(target=target):
                session = Session(id="session-1")
                session.transition_to(
                    SessionStatus.RUNNING,
                    self.make_event(SessionEventType.STARTED),
                )
                terminal_event = self.make_event(event_kind)

                session.transition_to(target, terminal_event)

                self.assertEqual(session.status, target)
                self.assertIs(session.events[-1], terminal_event)

    def test_record_event(self):
        session = Session(id="session-1")
        event = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id=session.id,
            reason="Session 创建完成",
        )

        session.record_event(event)

        self.assertEqual(session.events, [event])

    def test_cannot_record_event_from_another_session(self):
        session = Session(id="session-1")
        event = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id="session-2",
            reason="Session 创建完成",
        )

        with self.assertRaises(ValueError):
            session.record_event(event)

        self.assertEqual(session.events, [])

    def test_record_event_rejects_state_transition_event(self):
        session = Session(id="session-1")
        event = self.make_event(SessionEventType.STARTED)

        with self.assertRaises(ValueError):
            session.record_event(event)

        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.events, [])

    def test_session_can_hold_run_record(self):
        session = Session(id="session-1")
        record = SessionRunRecord(
            run_id="run-1",
            status="blocked",
            started_at=datetime.now(timezone.utc),
            final_reason="max_rounds_exceeded",
        )

        session.run_records.append(record)

        self.assertEqual(session.run_records, [record])

    def test_sessions_do_not_share_run_records(self):
        first = Session(id="session-1")
        second = Session(id="session-2")

        first.run_records.append(
            SessionRunRecord(
                run_id="run-1",
                status="completed",
                started_at=datetime.now(timezone.utc),
            )
        )

        self.assertEqual(second.run_records, [])
