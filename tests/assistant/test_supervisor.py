from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
import tempfile
import time
import unittest

import pytest

from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.subagent.subagent import project_delegations, project_reclaimed
from helperme.paths import HelperMeHome
from helperme.runtime import SqliteJournal
from tests.fixtures.session_worker import (
    cancellable_config,
    config_for,
    interrupted_read_config,
    failing_startup_config,
    failing_request_config,
)

pytestmark = pytest.mark.process


async def until(predicate, timeout=30):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


class SupervisorTest(unittest.IsolatedAsyncioTestCase):
    async def test_user_image_refs_cross_the_worker_boundary(self):
        import json
        from PIL import Image
        from io import BytesIO
        from helperme.assistant.attachments import AttachmentGateway

        buffer = BytesIO()
        Image.new("RGB", (16, 16), "red").save(buffer, format="PNG")
        await self.host.create("image-session")
        ref = AttachmentGateway(self.home.runtime_sessions_root).for_session(
            "image-session"
        ).save_image(buffer.getvalue(), "image/png")
        await self.host.accept_input(
            "image-session",
            "[Image #1]",
            artifact_refs=(ref.attachment_id,),
            delivery_id="image-input",
        )
        await until(lambda: ("image-session", "done") in self.output)
        received = json.loads(
            (self.root / "received-images.json").read_text(encoding="utf-8")
        )
        self.assertEqual(received[0]["id"], ref.attachment_id)

    async def persist_child(self):
        from datetime import datetime, timezone
        from helperme.runtime.events import (
            DomainFactCommitted,
            EventDraft,
            DeliveryIdentity,
        )

        await self.store.create("child")
        journal = SqliteJournal(self.store.require("child"))
        await journal.accept_delivery(
            EventDraft(
                event_id="task-event",
                session_id="child",
                payload=DomainFactCommitted(
                    "subagent.task", {"task": "read", "parent_session_id": "parent"}
                ),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity("subagent", "task"),
            )
        )
        return journal

    async def assert_failure_report(self, message):
        from helperme.runtime import DomainFactCommitted

        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertEqual(failure.failure.exception_type, "builtins.RuntimeError")
        self.assertIn(message, failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        reports = [
            e.payload
            for e in events
            if isinstance(e.payload, DomainFactCommitted)
            and e.payload.fact_type == "subagent.report"
        ]
        self.assertEqual(len(reports), 1)
        self.assertIn(message, reports[0].data["failure"])

    async def test_failed_reader_still_reports_and_exits(self):
        from helperme.assistant.host.ipc import WorkerFailed

        await self.host.create("parent")
        await self.persist_child()
        self.host.config_factory = partial(failing_request_config, self.root)
        with self.assertRaises(WorkerFailed):
            await asyncio.wait_for(
                self.host.resolve_authorizations("child", approved=True), 30
            )
        await self.assert_failure_report("application request failed")

    async def test_new_child_has_parent_identity_before_initialization(self):
        await self.host.create("parent")
        self.host.config_factory = partial(failing_startup_config, self.root, "config")
        await asyncio.wait_for(
            self.host._route(
                "create_child",
                "child",
                dict(
                    fact_type="subagent.task",
                    data={"task": "read", "parent_session_id": "parent"},
                    source="subagent",
                    delivery_id="task",
                    requests_decision=True,
                ),
            ),
            30,
        )
        await self.assert_failure_report("config initialization failed")

    async def test_child_startup_failure_does_not_strand_parent_delegate(self):
        from tests.fixtures.session_worker import delegate_startup_failure_config

        self.host.config_factory = partial(delegate_startup_failure_config, self.root)
        await self.host.create("parent")
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        for _ in range(2):
            failure = await asyncio.wait_for(self.host.wait_failure(), 30)
            self.assertIn("/sub-", failure.session_id)
            self.assertIn("child initialization failed", failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_delegations(events)), 2)
        self.assertEqual(len(project_reclaimed(events)), 2)
        self.assertTrue(self.host.failures.empty())
        self.assertIn(("parent", "done"), self.output)

    async def assert_startup_failure(self, stage):
        from helperme.assistant.host.ipc import WorkerFailed

        await self.host.create("parent")
        await self.persist_child()
        self.host.config_factory = partial(failing_startup_config, self.root, stage)
        with self.assertRaises(WorkerFailed):
            await asyncio.wait_for(self.host.resume("child"), 30)
        await self.assert_failure_report(f"{stage} initialization failed")

    async def test_config_initialization_failure_is_reported(self):
        await self.assert_startup_failure("config")

    async def test_assembly_initialization_failure_is_reported(self):
        await self.assert_startup_failure("assembly")

    async def test_client_initialization_failure_is_reported(self):
        await self.assert_startup_failure("client")

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.home = HelperMeHome(self.root / "home")
        self.store = SessionStore(self.home.runtime_sessions_root)
        self.output = []
        self.output_ids = []
        self.previews = []
        self.delivery_order = []
        self.host = self.new_host()

    def new_host(self):
        def deliver(session_id, output_id, text):
            self.output.append((session_id, text))
            self.output_ids.append(output_id)
            self.delivery_order.append(("final", session_id, output_id, text))

        def preview(*values):
            self.previews.append(values)
            self.delivery_order.append(("preview", *values))

        return HostSupervisor(
            self.store,
            partial(config_for, self.root),
            self.home,
            deliver,
            preview_sink=preview,
        )

    async def asyncTearDown(self):
        await self.host.close()
        self.directory.cleanup()

    async def test_idle_exit_delivery_and_explicit_restart(self):
        await self.host.create("one")
        await until(lambda: not self.host.workers and not self.host.watchers)
        await self.host.receive_user_message("one", "hello", delivery_id="input")
        await until(lambda: self.output)
        await until(lambda: not self.host.workers and not self.host.watchers)
        await self.host.close()
        self.host = self.new_host()
        self.assertEqual(self.host.workers, {})
        view = await self.host.resume("one")
        self.assertEqual(view.status, "waiting")
        await self.host.receive_user_message("one", "hello", delivery_id="input")
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertEqual(self.output, [("one", "done")])

    async def test_preview_and_final_cross_worker_boundary_in_order(self):
        await self.host.create("one")
        await self.host.receive_user_message("one", "hello", delivery_id="input")
        await until(lambda: self.output == [("one", "done")])

        started, delta, final = self.delivery_order
        self.assertEqual(started[:3], ("preview", "one", "started"))
        self.assertEqual(delta[:3], ("preview", "one", "delta"))
        self.assertEqual(delta[4], "done")
        self.assertEqual(final[:2], ("final", "one"))
        self.assertEqual(started[3], delta[3])
        self.assertEqual(delta[3], final[2])

    async def test_selected_idle_worker_stays_until_owner_releases_it(self):
        await self.host.create("one")
        view = await self.host.select("cli", "one")
        self.assertEqual(view.status, "waiting")
        await until(
            lambda: "one" in self.host.workers
            and self.host.workers["one"].idle_revision is not None
        )
        process_id = self.host.workers["one"].process.pid

        await self.host.receive_user_message("one", "hello", delivery_id="input")
        await until(lambda: self.output == [("one", "done")])
        await until(lambda: self.host.workers["one"].idle_revision is not None)
        self.assertIn("one", self.host.workers)
        self.assertEqual(self.host.workers["one"].process.pid, process_id)

        await self.host.select("web", "one")
        await self.host.release("cli")
        self.assertIn("one", self.host.workers)
        await self.host.release("web")
        await until(lambda: "one" not in self.host.workers)

    async def test_decision_cancel_is_cooperative_and_durable_across_worker_boundary(self):
        from helperme.runtime import DecisionCancelled

        self.host.config_factory = partial(cancellable_config, self.root)
        await self.host.create("one")
        await self.host.select("acp", "one")
        await self.host.accept_input(
            "one",
            "CANCEL_PROCESS",
            delivery_id="input",
        )
        await until(lambda: (self.root / "cancel-started").exists())

        await self.host.cancel_turn("one")
        view = await self.host.wait_quiescent("one")

        self.assertTrue((self.root / "cancel-observed").exists())
        self.assertEqual(view.status, "waiting")
        events = await SqliteJournal(self.store.require("one")).snapshot("one")
        self.assertIsInstance(events[-1].payload, DecisionCancelled)

    async def test_unselected_busy_worker_stops_only_after_work_finishes(self):
        await self.host.create("one")
        await self.host.select("cli", "one")
        await self.host.receive_user_message(
            "one", "BLOCK_PROCESS", delivery_id="input"
        )
        await until(lambda: list(self.root.glob("blocked-*")))

        await self.host.release("cli")
        self.assertIn("one", self.host.workers)

        (self.root / "release").touch()
        await until(lambda: ("one", "done") in self.output)
        await until(lambda: "one" not in self.host.workers)

    async def test_failed_selection_keeps_previous_owner_mapping(self):
        from helperme.assistant.host.ipc import WorkerFailed

        await self.host.create("old")
        await self.host.select("cli", "old")
        await self.host.create("broken")
        self.host.config_factory = partial(
            failing_startup_config, self.root, "config"
        )

        with self.assertRaises(WorkerFailed):
            await self.host.select("cli", "broken")

        self.assertEqual(self.host.selections["cli"], "old")
        self.assertIn("old", self.host.workers)

    async def test_blocking_worker_and_crash_do_not_stop_another(self):
        await self.host.create("blocked")
        await self.host.receive_user_message(
            "blocked", "BLOCK_PROCESS", delivery_id="a"
        )
        await until(lambda: list(self.root.glob("blocked-*")))
        blocked_at = time.monotonic()
        await self.host.create("crash")
        await self.host.receive_user_message("crash", "CRASH_PROCESS", delivery_id="b")
        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertEqual(failure.failure.exception_type, "builtins.RuntimeError")
        self.assertIn("intentional worker crash", failure.failure.traceback)
        await self.host.create("healthy")
        await self.host.receive_user_message("healthy", "hello", delivery_id="c")
        await until(lambda: ("healthy", "done") in self.output)
        self.assertIn("blocked", self.host.workers)
        await asyncio.sleep(max(0, 31 - (time.monotonic() - blocked_at)))
        (self.root / "release").touch()
        await until(lambda: ("blocked", "done") in self.output)

    async def test_two_children_return_after_parent_worker_exits(self):
        await self.host.create("parent")
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        await until(lambda: len(list(self.root.glob("blocked-*"))) == 2)
        await until(lambda: "parent" not in self.host.workers)
        pids = {worker.process.pid for worker in self.host.workers.values()}
        self.assertEqual(len(pids), 2)
        (self.root / "release").touch()
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_delegations(events)), 2)
        self.assertEqual(len(project_reclaimed(events)), 2)
        self.assertTrue(all(sid == "parent" for sid, _ in self.output))

    async def test_resume_selected_parent_recovers_its_children_only(self):
        await self.host.create("unrelated")
        await self.host.create("parent")
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        await until(lambda: len(list(self.root.glob("blocked-*"))) == 2)
        await until(lambda: "parent" not in self.host.workers)
        await self.host.close()
        (self.root / "release").touch()
        self.host = self.new_host()
        self.assertEqual(self.host.workers, {})
        await asyncio.wait_for(self.host.resume("parent"), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_reclaimed(events)), 2)
        self.assertEqual(
            await SqliteJournal(self.store.require("unrelated")).snapshot("unrelated"),
            (),
        )
        self.assertTrue(self.host.failures.empty())

    async def test_saved_return_is_redelivered_after_host_restart(self):
        route = self.host._route

        async def interrupted_route(operation, session_id, arguments):
            if operation == "fact" and session_id == "parent":
                raise RuntimeError("interrupted before parent acceptance")
            return await route(operation, session_id, arguments)

        self.host._route = interrupted_route
        (self.root / "release").touch()
        await self.host.create("parent")
        await self.host.receive_user_message(
            "parent", "DELEGATE_CHILDREN", delivery_id="input"
        )
        await asyncio.wait_for(self.host.wait_failure(), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        await self.host.close()
        self.host = self.new_host()
        await asyncio.wait_for(self.host.resume("parent"), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(len(project_reclaimed(events)), 2)
        self.assertTrue(self.host.failures.empty())

    async def test_deliveries_during_idle_transition_are_all_durable(self):
        await self.host.create("one")
        await asyncio.gather(
            *(
                self.host.receive_user_message(
                    "one", f"message {i}", delivery_id=f"input-{i}"
                )
                for i in range(12)
            )
        )
        await until(lambda: not self.host.workers and not self.host.watchers)
        from helperme.runtime import UserMessageReceived

        events = await SqliteJournal(self.store.require("one")).snapshot("one")
        self.assertEqual(
            len([e for e in events if isinstance(e.payload, UserMessageReceived)]), 12
        )
        self.assertTrue(self.host.failures.empty())

    async def test_child_recovery_preserves_unfinished_read_and_reports_once(self):
        from helperme.runtime import DispatchAttemptStarted
        from helperme.assistant.subagent.subagent import TASK_FACT

        self.host.config_factory = partial(interrupted_read_config, self.root)
        (self.root / "release").touch()
        await self.host.create("parent")
        await self.host._route(
            "create_child",
            "child",
            dict(
                fact_type=TASK_FACT,
                data={"task": "READ_THEN_REPORT", "parent_session_id": "parent"},
                source="subagent",
                delivery_id="task",
                requests_decision=True,
            ),
        )
        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertEqual(failure.failure.exception_type, "ProcessExit")
        self.assertIn("exit code 1", failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        journal = SqliteJournal(self.store.require("child"))
        before = await journal.snapshot("child")
        started = next(
            e.event_id for e in before if isinstance(e.payload, DispatchAttemptStarted)
        )
        await asyncio.wait_for(self.host.resume("child"), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        after = await journal.snapshot("child")
        self.assertEqual(after[:len(before)], before)
        self.assertIn(started, {e.event_id for e in after})
        self.assertFalse((self.root / "read-retried").exists())
        from helperme.runtime import CommandPhase, DomainFactCommitted, StateProjector
        from helperme.assistant.subagent.subagent import REPORT_FACT, RETURN_FACT
        state = StateProjector().project("child", after).state
        self.assertEqual(state.commands[0].phase, CommandPhase.UNKNOWN)
        self.assertEqual(sum(
            isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == RETURN_FACT
            for e in after
        ), 1)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(project_reclaimed(events), frozenset({"child"}))
        report = next(e for e in events if isinstance(e.payload, DomainFactCommitted)
                      and e.payload.fact_type == REPORT_FACT)
        self.assertTrue(report.payload.requests_decision)
        self.assertIn("执行结果未知", report.payload.data["failure"])
        self.assertIn(state.commands[0].command.command_id, report.payload.data["failure"])
        await asyncio.wait_for(self.host.resume("child"), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertEqual(await journal.snapshot("child"), after)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(sum(
            isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == REPORT_FACT
            for e in events
        ), 1)
        self.assertTrue(self.host.failures.empty())

    async def test_child_unexpected_crash_is_reported_then_still_exposed(self):
        from helperme.runtime import DomainFactCommitted
        from helperme.assistant.subagent.subagent import REPORT_FACT, TASK_FACT

        await self.host.create("parent")
        await self.host._route(
            "create_child",
            "child",
            dict(
                fact_type=TASK_FACT,
                data={"task": "CRASH_PROCESS", "parent_session_id": "parent"},
                source="subagent",
                delivery_id="task",
                requests_decision=True,
            ),
        )
        failure = await asyncio.wait_for(self.host.wait_failure(), 30)
        self.assertIn("intentional worker crash", failure.failure.message)
        await until(lambda: not self.host.workers and not self.host.watchers)
        events = await SqliteJournal(self.store.require("parent")).snapshot("parent")
        self.assertEqual(project_reclaimed(events), frozenset({"child"}))
        report = next(
            event.payload
            for event in events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == REPORT_FACT
        )
        self.assertIs(report.data["reported"], False)
        self.assertIn("intentional worker crash", report.data["failure"])
        self.assertIn("Traceback", report.data["failure"])

    async def test_parent_reclaim_stops_child_and_does_not_resume_it(self):
        from helperme.runtime import DomainFactCommitted
        from helperme.assistant.subagent.subagent import REPORT_FACT, RETURN_FACT, TASK_FACT

        await self.host.create("parent")
        await self.host._route(
            "create_child",
            "child",
            dict(
                fact_type=TASK_FACT,
                data={"task": "read something", "parent_session_id": "parent"},
                source="subagent",
                delivery_id="task",
                requests_decision=True,
            ),
        )
        await until(lambda: list(self.root.glob("blocked-*")))
        await self.host._route(
            "reclaim_child",
            "child",
            {"parent_session_id": "parent", "reason": "stop"},
        )
        await until(lambda: "child" not in self.host.workers)
        await until(lambda: not self.host.watchers or "child" not in self.host.workers)
        self.assertTrue(self.host.failures.empty())

        child_events = await SqliteJournal(self.store.require("child")).snapshot(
            "child"
        )
        returned = next(
            event.payload
            for event in child_events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == RETURN_FACT
        )
        self.assertIs(returned.data["cancelled"], True)
        self.assertEqual(returned.data["reason"], "stop")

        parent_events = await SqliteJournal(self.store.require("parent")).snapshot(
            "parent"
        )
        report = next(
            event.payload
            for event in parent_events
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == REPORT_FACT
        )
        self.assertIs(report.requests_decision, False)
        self.assertIs(report.data["cancelled"], True)
        self.assertEqual(project_reclaimed(parent_events), frozenset({"child"}))

        await asyncio.wait_for(self.host.resume("parent"), 30)
        await until(lambda: not self.host.workers and not self.host.watchers)
        self.assertNotIn("child", self.host.workers)
        self.assertTrue(self.host.failures.empty())
