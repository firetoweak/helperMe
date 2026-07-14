import unittest
from datetime import datetime, timezone

from core.session_state import (
    InvalidSessionTransition,
    Session,
    SessionEvent,
    SessionEventSource,
    SessionEventType,
    SessionRunRecord,
    SessionStatus,
)


class SessionStateTest(unittest.TestCase):
    def test_new_session_is_pending(self):
        session = Session(id="session-1")

        self.assertEqual(session.status, SessionStatus.PENDING)

    def test_sessions_do_not_share_conversation(self):
        first = Session(id="session-1")
        second = Session(id="session-2")

        self.assertIsNot(first.conversation, second.conversation)

    def test_interrupt_and_resume(self):
        session = Session(id="session-1")

        session.transition_to(SessionStatus.RUNNING)
        session.transition_to(SessionStatus.INTERRUPTED)
        session.transition_to(SessionStatus.RUNNING)

        self.assertEqual(session.status, SessionStatus.RUNNING)

    def test_pending_cannot_be_interrupted(self):
        session = Session(id="session-1")

        with self.assertRaises(InvalidSessionTransition):
            session.transition_to(SessionStatus.INTERRUPTED)

        self.assertEqual(session.status, SessionStatus.PENDING)

    def test_record_event(self):
        session = Session(id="session-1")
        event = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id=session.id,
            source=SessionEventSource.RUNTIME,
            reason="Session 创建完成",
        )

        session.record_event(event)

        self.assertEqual(session.events, [event])

    def test_cannot_record_event_from_another_session(self):
        session = Session(id="session-1")
        event = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id="session-2",
            source=SessionEventSource.RUNTIME,
            reason="Session 创建完成",
        )

        with self.assertRaises(ValueError):
            session.record_event(event)

        self.assertEqual(session.events, [])

    def test_events_do_not_share_data(self):
        first = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id="session-1",
            source=SessionEventSource.RUNTIME,
            reason="创建第一个 Session",
        )
        second = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id="session-2",
            source=SessionEventSource.RUNTIME,
            reason="创建第二个 Session",
        )

        self.assertIsNot(first.data, second.data)

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
