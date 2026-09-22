from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from helperme.assistant.auto_authorize import AutoAuthorizeStore
from helperme.assistant.host.supervisor import HostSupervisor


class AutoAuthorizeStoreTest(unittest.TestCase):
    def test_missing_key_is_false_and_only_explicit_toggle_is_written(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = AutoAuthorizeStore(root)

            self.assertFalse(store.get("session-1"))
            self.assertFalse((root / "auto_authorize.json").is_file())

            store.remember("session-1", False)
            self.assertFalse((root / "auto_authorize.json").is_file())

            store.set("session-1", True)
            raw = json.loads((root / "auto_authorize.json").read_text(encoding="utf-8"))
            self.assertEqual(raw, {"session-1": True})
            self.assertTrue(AutoAuthorizeStore(root).get("session-1"))


class HostAutoAuthorizeTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_policy_comes_only_from_session_store(self):
        host = object.__new__(HostSupervisor)
        host._auto_authorize = AutoAuthorizeStore(None)
        host._auto_authorize.remember("enabled", True)
        host.selections = {
            "tui": "enabled",
            "telegram:chat": "disabled",
        }
        host.request = AsyncMock()

        await host._push_auto_authorize("enabled")
        await host._push_auto_authorize("disabled")

        self.assertEqual(
            host.request.await_args_list,
            [
                call(
                    "apply_auto_authorize",
                    "enabled",
                    {"enabled": True},
                ),
                call(
                    "apply_auto_authorize",
                    "disabled",
                    {"enabled": False},
                ),
            ],
        )

    async def test_set_auto_authorize_writes_host_store_and_pushes_policy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            host = object.__new__(HostSupervisor)
            host._auto_authorize = AutoAuthorizeStore(root)
            host._push_auto_authorize = AsyncMock(
                return_value=SimpleNamespace(auto_authorize=False)
            )
            host._with_host_metadata = lambda observed, session_id: observed

            view = await host.set_auto_authorize("session-1", True)

            self.assertTrue(host.auto_authorize("session-1"))
            self.assertEqual(
                json.loads((root / "auto_authorize.json").read_text(encoding="utf-8")),
                {"session-1": True},
            )
            host._push_auto_authorize.assert_awaited_once_with("session-1")
            self.assertIs(view, host._push_auto_authorize.return_value)
