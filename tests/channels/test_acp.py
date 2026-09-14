from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from acp import PROTOCOL_VERSION, RequestError

from helperme.channels.acp import HelperMeAcpAgent
from helperme.config import WorkspaceConfig


class _Sessions:
    def __init__(self) -> None:
        self.calls = []
        self.accepted = asyncio.Event()
        self.quiescent = asyncio.Event()

    async def create(self, session_id):
        self.calls.append(("create", session_id))

    async def select(self, owner, session_id):
        self.calls.append(("select", owner, session_id))
        return SimpleNamespace()

    async def accept_input(self, session_id, content, **kwargs):
        self.calls.append(("accept_input", session_id, content, kwargs))
        self.accepted.set()
        return SimpleNamespace(control_message=None)

    async def wait_quiescent(self, session_id):
        await self.quiescent.wait()
        return SimpleNamespace()

    async def cancel_turn(self, session_id):
        self.calls.append(("cancel_turn", session_id))
        return SimpleNamespace()

    async def release(self, owner):
        self.calls.append(("release", owner))


class _Client:
    def __init__(self) -> None:
        self.updates = []

    async def session_update(self, **kwargs):
        self.updates.append(kwargs)


class AcpChannelTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.sessions = _Sessions()
        self.client = _Client()
        self.agent = HelperMeAcpAgent(
            self.sessions,
            WorkspaceConfig(self.root, False),
        )
        self.agent.on_connect(self.client)
        await self.agent.initialize(PROTOCOL_VERSION)

    async def asyncTearDown(self) -> None:
        await self.agent.close()
        self.directory.cleanup()

    async def _new_session(self) -> str:
        response = await self.agent.new_session(str(self.root), mcp_servers=[])
        return response.session_id

    async def test_prompt_streams_output_until_session_is_quiescent(self) -> None:
        from acp.schema import TextContentBlock

        session_id = await self._new_session()
        prompt = asyncio.create_task(
            self.agent.prompt(
                session_id,
                [TextContentBlock(type="text", text="hello")],
            )
        )
        await self.sessions.accepted.wait()

        await self.agent.deliver(session_id, "world")
        self.sessions.quiescent.set()
        response = await prompt

        self.assertEqual(response.stop_reason, "end_turn")
        self.assertEqual(
            self.client.updates[0]["update"].content.text,
            "world",
        )
        accepted = next(call for call in self.sessions.calls if call[0] == "accept_input")
        self.assertEqual(accepted[2], "hello")
        self.assertEqual(accepted[3]["source"], "acp")

    async def test_each_delivery_is_its_own_agent_message(self) -> None:
        from acp.schema import TextContentBlock

        session_id = await self._new_session()
        prompt = asyncio.create_task(
            self.agent.prompt(
                session_id,
                [TextContentBlock(type="text", text="hello")],
            )
        )
        await self.sessions.accepted.wait()

        await self.agent.deliver(session_id, "first")
        await self.agent.deliver(session_id, "second")
        self.sessions.quiescent.set()
        await prompt

        first, second = (item["update"] for item in self.client.updates[:2])
        self.assertEqual(first.content.text, "first")
        self.assertEqual(second.content.text, "second")
        self.assertNotEqual(first.message_id, second.message_id)

    async def test_cancel_stops_decision_before_returning_cancelled(self) -> None:
        from acp.schema import TextContentBlock

        session_id = await self._new_session()
        prompt = asyncio.create_task(
            self.agent.prompt(
                session_id,
                [TextContentBlock(type="text", text="wait")],
            )
        )
        await self.sessions.accepted.wait()

        await self.agent.cancel(session_id)
        response = await prompt

        self.assertEqual(response.stop_reason, "cancelled")
        self.assertIn(("cancel_turn", session_id), self.sessions.calls)

        await self.agent.deliver(session_id, "late output")
        self.assertEqual(self.client.updates, [])

    async def test_new_session_rejects_workspace_escape(self) -> None:
        outside = self.root.parent

        with self.assertRaises(RequestError) as raised:
            await self.agent.new_session(str(outside), mcp_servers=[])

        self.assertEqual(raised.exception.code, -32602)
        self.assertEqual(self.sessions.calls, [])

    async def test_usage_is_projected_as_session_update(self) -> None:
        session_id = await self._new_session()

        await self.agent.report_usage(session_id, 1200, 240000)

        update = self.client.updates[-1]["update"]
        self.assertEqual(update.session_update, "usage_update")
        self.assertEqual(update.used, 1200)
        self.assertEqual(update.size, 240000)

    async def test_tool_progress_is_projected_as_session_updates(self) -> None:
        session_id = await self._new_session()

        await self.agent.report_tool(
            session_id, "start", "command-1", "read_file", {"path": "a.txt"}
        )
        await self.agent.report_tool(
            session_id,
            "finish",
            "command-1",
            "read_file",
            {"ok": False, "code": "NOT_FOUND", "error": "missing"},
        )

        start, finish = (item["update"] for item in self.client.updates[-2:])
        self.assertEqual(start.session_update, "tool_call")
        self.assertEqual(start.kind, "read")
        self.assertEqual(start.status, "in_progress")
        self.assertEqual(finish.session_update, "tool_call_update")
        self.assertEqual(finish.status, "failed")
        self.assertEqual(finish.content[0].content.text, "missing")


if __name__ == "__main__":
    unittest.main()
