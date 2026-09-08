from __future__ import annotations

import asyncio
from functools import partial
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from helperme.assistant.compact import CompactContext, CONTINUED, save_document
from helperme.assistant.compact_host import seed_fact
from helperme.assistant.compact_store import CompactStore
from helperme.assistant.session_store import SessionStore
from helperme.assistant.supervisor import HostSupervisor
from helperme.assistant.context.projection import ModelContextProjector
from helperme.assistant.artifacts import FileArtifactGateway
from helperme.paths import HelperMeHome
from helperme.runtime import SqliteJournal, StepCommitted, DomainFactCommitted
from tests.fixtures.compact_worker import config_for, tool_config, HANDOFF


async def until(predicate, timeout=60):
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
        self.statuses = []
        self.host = self.new_host()

    def new_host(self):
        return HostSupervisor(
            self.store,
            partial(config_for, self.root),
            self.home,
            lambda sid, text: self.outputs.append((sid, text)),
            conversation_status_sink=self.statuses.append,
        )

    async def asyncTearDown(self):
        async with asyncio.timeout(15):
            await self.host.close()
        self.temp.cleanup()

    async def test_background_rollover_preserves_tail_idle_and_delivery_identity(self):
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        await until(
            lambda: (self.root / "compact_started").exists() and len(self.outputs) == 1
        )
        await self.host.receive_user_message("chat", "TAIL_KEEP", delivery_id="tail")
        await until(lambda: len(self.outputs) == 2)
        self.assertEqual(self.host.compact.store.binding("chat")[1], "chat")
        (self.root / "release_compact").touch()
        await until(lambda: self.host.compact.store.binding("chat")[1] != "chat")
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertTrue(self.host.failures.empty())
        self.assertEqual(len(self.outputs), 2)  # idle stays idle
        successor = self.host.compact.store.binding("chat")[1]
        self.assertEqual(
            [(s.session_id, s.compact_count, s.compact_phase) for s in self.statuses],
            [("chat", 0, "running"), ("chat", 0, "ready"), (successor, 1, None)],
        )
        old_events = await SqliteJournal(self.store.require("chat")).snapshot("chat")
        new_events = await SqliteJournal(self.store.require(successor)).snapshot(
            successor
        )
        self.assertEqual(len(new_events), 1)
        self.assertIsInstance(new_events[0].payload, DomainFactCommitted)
        self.assertEqual(new_events[0].payload.fact_type, CONTINUED)
        self.assertFalse(new_events[0].payload.requests_decision)
        # A retry addressed to the original conversation must not become a new message in S1.
        await self.host.receive_user_message("chat", "TAIL_KEEP", delivery_id="tail")
        await self.host.close()
        self.host = self.new_host()
        self.assertEqual(self.host.conversation_status(successor), self.statuses[-1])
        await self.host.receive_user_message("chat", "继续", delivery_id="next")
        await until(lambda: len(self.outputs) == 3)
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertTrue(self.host.failures.empty())
        self.assertEqual([sid for sid, _ in self.outputs], ["chat"] * 3)
        self.assertEqual(
            await SqliteJournal(self.store.require("chat")).snapshot("chat"), old_events
        )
        requests = [
            json.loads(line)
            for line in (self.root / "requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertIn("TAIL_KEEP", json.dumps(requests[-1], ensure_ascii=False))
        self.assertIn(
            "模型生成的交接材料", json.dumps(requests[-1], ensure_ascii=False)
        )
        self.assertEqual(
            sum(
                isinstance(e.payload, StepCommitted)
                for e in await SqliteJournal(self.store.require(successor)).snapshot(
                    successor
                )
            ),
            1,
        )

    async def test_prepared_cutover_finishes_after_restart_before_accepting_input(self):
        await self.store.create("old")
        registry = self.host.compact.store
        registry.register("old")
        gateway = self.host.compact.gateway
        bundle = save_document(
            gateway, "old", {"records": [], "raw": {}, "artifacts": [], "previous": []}
        )
        context = save_document(
            gateway, "old", {"messages": [{"role": "user", "content": HANDOFF}]}
        )
        job = registry.start("old", 1, {"artifact": bundle})
        registry.finish(job["reader"], HANDOFF)
        registry.prepare(
            "old",
            seed_fact(
                CONTINUED,
                {
                    "source": "old",
                    "bundle": bundle,
                    "upto": 1,
                    "cutover": 1,
                    "context": context,
                    "pending": [],
                },
                continuing=False,
            ),
        )
        await self.host.close()
        self.host = self.new_host()
        await self.host.receive_user_message("old", "after restart", delivery_id="new")
        await until(lambda: self.outputs)
        self.assertEqual(self.host.compact.store.binding("old")[1], job["successor"])
        self.assertEqual(
            await SqliteJournal(self.store.require("old")).snapshot("old"), ()
        )

    async def test_capacity_wait_keeps_accepting_messages_then_continues(self):
        async with asyncio.timeout(45):
            await self.capacity_wait_scenario()

    async def capacity_wait_scenario(self):
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        await until(
            lambda: (self.root / "compact_started").exists() and len(self.outputs) == 1
        )
        await self.host.receive_user_message(
            "chat", " detail" * 35000, delivery_id="large-tail"
        )
        # Input admission completes even though another model request cannot fit.
        await self.host.receive_user_message(
            "chat", "LATEST_REQUIREMENT", delivery_id="latest"
        )
        view = await self.host.view("chat")
        self.assertEqual(view.status, "runnable")
        self.assertEqual(len(self.outputs), 1)
        (self.root / "release_compact").touch()
        await until(lambda: self.host.compact.store.binding("chat")[1] != "chat")
        await until(lambda: len(self.outputs) == 2)
        requests = [
            json.loads(line)
            for line in (self.root / "requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertIn("LATEST_REQUIREMENT", json.dumps(requests[-1]))
        self.assertIn(" detail" * 35000, json.dumps(requests[-1]))
        self.assertTrue(self.host.failures.empty())

    async def test_compactor_failure_is_exposed_and_persisted_without_auto_retry(self):
        (self.root / "fail_compact").touch()
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        failure = await asyncio.wait_for(self.host.wait_failure(), 60)
        self.assertEqual(
            failure.failure.exception_type, "helperme.llm.api.LLMProviderError"
        )
        self.assertIn("compactor provider failed", failure.failure.message)
        job = self.host.compact.store.job("chat")
        self.assertIsNotNone(job["failure"])
        self.assertIsNone(job["summary"])
        self.assertEqual(self.statuses[-1].compact_phase, "failed")
        self.assertEqual(self.statuses[-1].compact_count, 0)
        self.assertEqual(self.host.compact.store.binding("chat")[1], "chat")
        from helperme.assistant.ipc import WorkerFailed

        with self.assertRaises(WorkerFailed):
            await self.host.compact.ensure_reader(job)

    async def test_tool_finishes_in_old_session_then_new_session_continues(self):
        self.host.config_factory = partial(tool_config, self.root)
        await self.host.create("chat")
        await self.host.receive_user_message(
            "chat", " history" * 31000, delivery_id="first"
        )
        await until(
            lambda: (self.root / "compact_started").exists() and len(self.outputs) == 1
        )
        await self.host.receive_user_message("chat", "RUN_TOOL", delivery_id="tool")
        await until(lambda: (self.root / "tool_started").exists())
        (self.root / "release_compact").touch()
        await until(lambda: self.host.compact.store.job("chat")["summary"] is not None)
        self.assertEqual(self.host.compact.store.binding("chat")[1], "chat")
        (self.root / "release_tool").touch()
        await until(lambda: self.host.compact.store.binding("chat")[1] != "chat")
        await until(lambda: len(self.outputs) == 2)
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertTrue(self.host.failures.empty())
        successor = self.host.compact.store.binding("chat")[1]
        new_events = await SqliteJournal(self.store.require(successor)).snapshot(
            successor
        )
        self.assertTrue(new_events[0].payload.requests_decision)
        self.assertEqual((self.root / "tool_count").read_text(), "executed\n")
        requests = [
            json.loads(line)
            for line in (self.root / "requests.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        tool_results = [m for m in requests[-1] if m["role"] == "tool"]
        self.assertEqual(len(tool_results), 1)
        self.assertIn("TOOL_EVIDENCE", tool_results[0]["content"])

    async def test_reading_is_bound_to_frozen_sources(self):
        await self.store.create("source")
        gateway = FileArtifactGateway(self.store.root)
        artifact = gateway.for_session("source").save("full tool output")
        bundle = save_document(
            gateway,
            "source",
            {
                "records": [],
                "raw": {"1": [{"role": "user", "content": "original"}]},
                "artifacts": [artifact.artifact_id],
                "previous": [],
            },
        )
        from helperme.assistant.compact import TASK

        await self.store.create(
            "reader",
            initial_fact=seed_fact(
                TASK,
                {
                    "source": "source",
                    "bundle": bundle,
                    "upto": 1,
                },
                continuing=True,
            ),
        )
        ctx = CompactContext(
            "reader",
            await SqliteJournal(self.store.require("reader")).snapshot("reader"),
            ModelContextProjector(gateway=gateway),
            None,
        )
        args = dict(
            source="source",
            kind="artifact",
            reference=artifact.artifact_id,
            offset=0,
            limit=100,
        )
        self.assertEqual((await ctx.read(None, args))["content"], "full tool output")
        self.assertEqual(
            (await ctx.read(None, {**args, "source": "unrelated"}))["error"],
            "SOURCE_NOT_AUTHORIZED",
        )
        self.assertEqual(
            (await ctx.read(None, {**args, "kind": "event", "reference": "2"}))[
                "error"
            ],
            "EVENT_NOT_IN_SOURCE",
        )

    async def test_capacity_wait_does_not_bypass_pending_authorization(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from helperme.assistant.compact import CompactBoundary
        from helperme.assistant.context.projection import ModelContextSettings
        from helperme.runtime import (
            AgentRuntime,
            MemoryJournal,
            ModelDecision,
            InvokeTool,
            ToolBinding,
        )

        class Decision:
            async def decide(self, frame):
                return ModelDecision(command_requests=(InvokeTool("protected", ()),))

        execute = AsyncMock()
        runtime = AgentRuntime(
            MemoryJournal(),
            Decision(),
            {"protected": ToolBinding(execute, requires_authorization=True)},
        )
        await runtime.create_session("source")
        await runtime.receive_user_message("source", "start", delivery_id="first")
        await runtime.advance("source")
        await runtime.receive_user_message(
            "source", " input" * 3000, delivery_id="later"
        )
        projector = ModelContextProjector(
            settings=ModelContextSettings(context_limit=1000)
        )
        context = CompactContext(
            "source", await runtime.snapshot("source"), projector, None
        )
        transport = AsyncMock()
        boundary = CompactBoundary(
            runtime,
            SimpleNamespace(
                _prompt_for=lambda frame: "", _schemas=lambda frame: ([], frozenset())
            ),
            context,
            config_for(self.root),
            None,
            transport,
        )
        self.assertFalse(await boundary.before_advance())
        execute.assert_not_called()
        transport.assert_not_called()
        state = await runtime.state("source")
        self.assertEqual(len(state.steps), 1)
        self.assertTrue(state.waiting_command_ids)
        await runtime.dispatcher.close()


class CompactStoreTest(unittest.TestCase):
    def test_incomplete_delivery_blocks_publication_and_conflicts_fail(self):
        with TemporaryDirectory() as directory:
            store = CompactStore(Path(directory))
            store.register("old")
            job = store.start("old", 1, {})
            store.finish(job["reader"], "summary")
            store.prepare("old", {"test": "prepared"})
            store.reserve_delivery("old", "user", "one", "old", "hello")
            with self.assertRaisesRegex(ValueError, "unaccepted"):
                store.publish("old", job["successor"])
            with self.assertRaisesRegex(ValueError, "conflicting"):
                store.reserve_delivery("old", "user", "one", "old", "different")
            store.acknowledge("old", "user", "one")
            store.publish("old", job["successor"])
            reopened = CompactStore(Path(directory))
            self.assertEqual(reopened.binding("old"), ("old", job["successor"]))
            self.assertEqual(reopened.status("old").compact_count, 1)
            self.assertEqual(
                reopened.reserve_delivery(
                    "old", "user", "one", job["successor"], "hello"
                ),
                ("old", True),
            )
            second = reopened.start(job["successor"], 1, {})
            reopened.finish(second["reader"], "second summary")
            reopened.prepare(job["successor"], {"test": "second prepared"})
            reopened.publish(job["successor"], second["successor"])
            reopened.publish(job["successor"], second["successor"])
            self.assertEqual(reopened.status("old").compact_count, 2)
            self.assertEqual(reopened.status(second["successor"]), reopened.status("old"))
            self.assertEqual(reopened.status("unrelated").compact_count, 0)
