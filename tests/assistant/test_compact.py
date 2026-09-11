from __future__ import annotations
import asyncio
from functools import partial
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pytest

from helperme.assistant.compact.core import (
    WINDOW,
)
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.artifacts import FileArtifactGateway
from helperme.paths import HelperMeHome
from helperme.runtime import SqliteJournal, StepCommitted, DomainFactCommitted
from tests.fixtures.compact_worker import config_for, HANDOFF

pytestmark = pytest.mark.process


async def until(predicate, timeout=45):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


class CompactTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = HelperMeHome(self.root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        self.outputs = []
        self.host = self.new_host()

    def new_host(self):
        return HostSupervisor(
            self.store,
            partial(config_for, self.root),
            self.home,
            lambda sid, text: self.outputs.append((sid, text)),
        )

    async def asyncTearDown(self):
        await self.host.close()
        self.temp.cleanup()

    async def start(self):
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        await until(
            lambda: (self.root / "compact_started").exists() and len(self.outputs) == 1
        )

    async def test_background_rollover_preserves_session_tail_and_delivery_identity(
        self,
    ):
        await self.start()
        job = self.host.compact.store.job("chat")
        material = json.loads(job["bundle"])
        frozen = json.loads(
            FileArtifactGateway(self.store.root)
            .for_session("chat")
            .read(material["inherited"], 0, 1000000)
            .content
        )
        request = json.loads(
            (self.root / "handoff_request.json").read_text(encoding="utf-8")
        )
        self.assertEqual(request["tools"], frozen["tools"])
        self.assertEqual(
            request["messages"][: len(frozen["messages"])], frozen["messages"]
        )
        self.assertNotIn("submit_handoff", str(request["tools"]))
        await self.host.receive_user_message("chat", "TAIL_KEEP", delivery_id="tail")
        await until(lambda: len(self.outputs) == 2)
        (self.root / "release_compact").touch()
        await until(lambda: self.host.conversation_status("chat").compact_count == 1)
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertTrue(self.host.failures.empty())
        self.assertEqual(len(self.outputs), 2)
        self.assertEqual(self.host.conversation_status("chat").session_id, "chat")
        events = await SqliteJournal(self.store.require("chat")).snapshot("chat")
        windows = [
            e
            for e in events
            if isinstance(e.payload, DomainFactCommitted)
            and e.payload.fact_type == WINDOW
        ]
        self.assertEqual(len(windows), 1)
        await self.host.receive_user_message("chat", "TAIL_KEEP", delivery_id="tail")
        await self.host.close()
        self.host = self.new_host()
        await self.host.receive_user_message("chat", "继续", delivery_id="next")
        await until(lambda: len(self.outputs) == 3)
        requests = [
            json.loads(line)
            for line in (self.root / "requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        last = json.dumps(requests[-1], ensure_ascii=False)
        self.assertIn("TAIL_KEEP", last)
        self.assertIn("模型生成的交接材料", last)
        self.assertNotIn(" history" * 100, last)
        after = await SqliteJournal(self.store.require("chat")).snapshot("chat")
        self.assertEqual(after[: len(events)], events)

    async def test_failure_keeps_old_window_and_does_not_restart(self):
        (self.root / "fail_compact").touch()
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        await until(
            lambda: self.host.conversation_status("chat").compact_phase == "failed"
        )
        job = self.host.compact.store.job("chat")
        await self.host.receive_user_message("chat", "继续", delivery_id="next")
        await until(lambda: len(self.outputs) == 2)
        self.assertEqual(self.host.conversation_status("chat").compact_count, 0)

    async def test_prepared_publication_is_recovered_without_new_session(self):
        await self.start()
        job = self.host.compact.store.job("chat")
        self.host.compact.store.finish(job["reader"], HANDOFF)
        snap = await self.host.request("compact_snapshot", "chat", {})
        self.host.compact.prepare_window(self.host.compact.store.job("chat"), snap)
        await self.host.close()
        self.host = self.new_host()
        await self.host.receive_user_message("chat", "恢复", delivery_id="recovered")
        await until(lambda: len(self.outputs) == 2)
        self.assertEqual(self.host.conversation_status("chat").compact_count, 1)
        self.assertEqual(self.host.conversation_status("chat").session_id, "chat")

    async def test_multiturn_reading_finishes_without_changing_tools(self):
        (self.root / "read_compact").touch()
        (self.root / "release_compact").touch()
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        await until(lambda: self.host.conversation_status("chat").compact_count == 1)
        with closing(self.host.compact.store.connect()) as db:
            job = dict(db.execute("SELECT * FROM compactions").fetchone())
        events = await SqliteJournal(self.store.require(job["reader"])).snapshot(
            job["reader"]
        )
        self.assertEqual(sum(isinstance(e.payload, StepCommitted) for e in events), 2)
        self.assertTrue(self.host.failures.empty())

    async def test_repeated_reads_are_reminded_and_can_complete(self):
        (self.root / "repeat_reads").touch()
        (self.root / "release_compact").touch()
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        await until(lambda: self.host.conversation_status("chat").compact_count == 1)
        with closing(self.host.compact.store.connect()) as db:
            job = dict(db.execute("SELECT * FROM compactions").fetchone())
        events = await SqliteJournal(self.store.require(job["reader"])).snapshot(job["reader"])
        steps = [e.payload for e in events if isinstance(e.payload, StepCommitted)]
        self.assertEqual(len(steps), 11)
        notices = [s.decision_metadata["loop_guard_notice"] for s in steps if s.decision_metadata]
        self.assertEqual(len(notices), 3)
        self.assertTrue(all(n["evidence"][0]["new_count"] == 3 for n in notices))
        self.assertTrue(self.host.failures.empty())

    async def test_visible_write_schema_does_not_authorize_handoff_execution(self):
        (self.root / "write_compact").touch()
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        await until(
            lambda: self.host.conversation_status("chat").compact_phase == "failed"
        )
        self.assertFalse((self.root / "forbidden.txt").exists())
        self.assertEqual(self.host.conversation_status("chat").compact_count, 0)
