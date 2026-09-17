from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.session_pause import SessionPauseStore


class SessionPauseStoreTest(unittest.TestCase):
    def test_missing_key_is_false_and_only_explicit_toggle_is_written(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionPauseStore(root)

            self.assertFalse(store.get("session-1"))
            self.assertFalse((root / "paused.json").is_file())

            store.remember("session-1", False)
            self.assertFalse((root / "paused.json").is_file())

            store.set("session-1", True)
            raw = json.loads((root / "paused.json").read_text(encoding="utf-8"))
            self.assertEqual(raw, {"session-1": True})
            self.assertTrue(SessionPauseStore(root).get("session-1"))


class HostRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_retry_clears_pause_otherwise_resumes(self):
        host = object.__new__(HostSupervisor)
        host._pause = SessionPauseStore(None)
        host.set_paused = AsyncMock(return_value="unpaused")
        host.resume = AsyncMock(return_value="resumed")

        self.assertEqual(await host.retry("session-1"), "resumed")
        host.resume.assert_awaited_once_with("session-1")
        host.set_paused.assert_not_awaited()

        host._pause.remember("session-1", True)
        self.assertEqual(await host.retry("session-1"), "unpaused")
        host.set_paused.assert_awaited_once_with("session-1", False)


class HostAcceptInputPauseTest(unittest.IsolatedAsyncioTestCase):
    async def test_accept_input_syncs_pause_from_worker_view(self):
        host = object.__new__(HostSupervisor)
        host._pause = SessionPauseStore(None)
        host._pause.remember("session-1", True)
        view = SimpleNamespace(paused=False)
        host.compact = SimpleNamespace(application=AsyncMock(return_value=view))
        host._with_preference = lambda observed, session_id: observed

        result = await host.accept_input("session-1", "hello")

        self.assertIs(result, view)
        self.assertFalse(host._pause.get("session-1"))
        host.compact.application.assert_awaited_once_with(
            "accept_input",
            "session-1",
            {"content": "hello"},
        )
