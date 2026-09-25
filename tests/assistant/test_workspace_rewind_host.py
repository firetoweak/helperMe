from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.session_metadata import SessionFlagStore
from helperme.assistant.workspace_versions import WorkspaceRewindFailed


def host_with(result):
    host = object.__new__(HostSupervisor)
    host._pause = SessionFlagStore(None, "paused.json")
    host._with_host_metadata = lambda observed, session_id: observed
    order: list[str] = []

    async def application(operation, session_id, arguments):
        order.append(operation)
        if operation == "rewind_workspace":
            return result
        return "view"

    async def wait_quiescent(session_id):
        order.append("quiescent")

    host.compact = SimpleNamespace(application=AsyncMock(side_effect=application))
    host.wait_quiescent = wait_quiescent
    return host, order


class HostRewindTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_session_is_held_and_stilled_before_files_move(self):
        """置暂停要早于打断，回退要等到进程真的停下来。

        反过来的话，本轮被取消后调度器还会往下走一步，文件在回退的同时被改。
        """
        host, order = host_with({"ok": True})

        self.assertEqual(await host.rewind_workspace("s", "step-1", "web-1"), "view")

        self.assertTrue(host.is_paused("s"))
        self.assertEqual(
            order,
            ["cancel_turn", "quiescent", "rewind_workspace", "view"],
        )

    async def test_a_failed_rewind_still_leaves_the_session_held(self):
        host, order = host_with({"ok": False, "error": "这一步没有成功的版本记录，无法回退。"})

        with self.assertRaises(WorkspaceRewindFailed):
            await host.rewind_workspace("s", "step-1", "web-1")

        self.assertTrue(host.is_paused("s"))
        self.assertNotIn("view", order)
