from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from helperme.assistant.auto_authorize import (
    AutoAuthorizeStore,
    auto_grant_for_owners,
)


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


class AutoGrantOwnersTest(unittest.TestCase):
    def test_web_reads_preference_other_owners_always_grant(self):
        self.assertFalse(auto_grant_for_owners((), False))
        self.assertFalse(auto_grant_for_owners(("web:c1",), False))
        self.assertTrue(auto_grant_for_owners(("web:c1",), True))
        self.assertTrue(auto_grant_for_owners(("tui",), False))
        self.assertTrue(auto_grant_for_owners(("tui", "web:c1"), False))
        self.assertTrue(auto_grant_for_owners(("telegram-bot-1-chat-2",), False))
