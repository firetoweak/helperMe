import unittest
from unittest.mock import Mock, patch

from core.session_runner import SessionRuntime
from core.session_state import (
    Session,
    SessionEventSource,
    SessionEventType,
    SessionStatus,
)


class SessionRuntimeCreateSessionTest(unittest.TestCase):
    def setUp(self):
        self.runtime = SessionRuntime(tools_runner=Mock())

    def test_create_session_registers_pending_session_with_created_event(self):
        session = self.runtime.create_session("session-1")

        self.assertIs(self.runtime.sessions["session-1"], session)
        self.assertEqual(session.status, SessionStatus.PENDING)
        self.assertEqual(session.run_records, [])
        self.assertEqual(len(session.events), 1)
        self.assertEqual(session.events[0].kind, SessionEventType.CREATED)
        self.assertEqual(session.events[0].source, SessionEventSource.RUNTIME)
        self.assertEqual(session.events[0].session_id, session.id)
        self.assertIsNone(session.events[0].run_id)

    def test_create_session_rejects_duplicate_id_without_replacing_original(self):
        original = self.runtime.create_session("session-1")

        with self.assertRaises(ValueError):
            self.runtime.create_session("session-1")

        self.assertIs(self.runtime.sessions["session-1"], original)
        self.assertEqual(len(self.runtime.sessions), 1)

    def test_create_session_requires_non_empty_id(self):
        for session_id in ("", "   "):
            with self.subTest(session_id=session_id):
                with self.assertRaises(ValueError):
                    self.runtime.create_session(session_id)

        self.assertEqual(self.runtime.sessions, {})

    def test_create_session_does_not_register_session_when_event_recording_fails(self):
        with patch.object(Session, "record_event", side_effect=ValueError("invalid event")):
            with self.assertRaises(ValueError):
                self.runtime.create_session("session-1")

        self.assertNotIn("session-1", self.runtime.sessions)


if __name__ == "__main__":
    unittest.main()
