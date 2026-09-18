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
    def test_entry_policy_for_auto_grant(self):
        # 无 owner：不替人放行。
        self.assertFalse(auto_grant_for_owners((), False))
        # Web：看 Session 总闸偏好。
        self.assertFalse(auto_grant_for_owners(("web:c1",), False))
        self.assertTrue(auto_grant_for_owners(("web:c1",), True))
        # TUI：不自动放行，等待 yes/no。
        self.assertFalse(auto_grant_for_owners(("tui",), False))
        self.assertFalse(auto_grant_for_owners(("tui",), True))
        # Web + TUI 混合：Web 优先，看总闸。
        self.assertFalse(auto_grant_for_owners(("tui", "web:c1"), False))
        self.assertTrue(auto_grant_for_owners(("tui", "web:c1"), True))
        # Telegram / ACP 等暂无授权交互入口的 Channel：保持放行。
        self.assertTrue(auto_grant_for_owners(("telegram-bot-1-chat-2",), False))
