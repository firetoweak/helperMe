import unittest

from helperme.assistant.artifacts import MemoryArtifactGateway
from helperme.assistant.compact.core import (
    CompactBoundary,
    CompactContext,
    TASK,
    save_document,
)
from helperme.assistant.context.projection import ModelContextProjector
from helperme.runtime import AgentRuntime, MemoryJournal, StateProjector
from helperme.assistant.toolsets import ToolSurface
from tests.assistant.test_toolsets import FakeEchoProvider


class WindowTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_is_limited_to_frozen_source_even_when_business_log_grows(self):
        gateway = MemoryArtifactGateway()
        projector = ModelContextProjector(gateway=gateway)
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("h")
        bundle = save_document(
            gateway,
            "b",
            {
                "records": [],
                "raw": {"1": [{"role": "user", "content": "before P"}]},
                "artifacts": [],
            },
        )
        request = save_document(
            gateway,
            "b",
            {
                "messages": [{"role": "system", "content": "fixed"}],
                "tools": [],
                "recent": [],
            },
        )
        await runtime.receive_domain_fact(
            "h",
            TASK,
            {
                "source": "b",
                "bundle": bundle,
                "inherited": request,
                "upto": 1,
                "window": None,
            },
            source="compact",
            delivery_id="task",
        )
        context = CompactContext("h", await runtime.snapshot("h"), projector, None)
        request_read = await context.read(
            None,
            {
                "source": "b",
                "kind": "artifact",
                "reference": request,
                "offset": 0,
                "limit": 1000,
            },
        )
        self.assertIn("fixed", request_read["data"]["content"])

        args = {
            "source": "b",
            "kind": "event",
            "reference": "1",
            "offset": 0,
            "limit": 1000,
        }
        self.assertIn("before P", (await context.read(None, args))["data"]["content"])
        self.assertEqual(
            (await context.read(None, {**args, "reference": "2"}))["error"],
            "EVENT_NOT_IN_SOURCE",
        )
        self.assertEqual(
            (await context.read(None, {**args, "source": "other"}))["error"],
            "SOURCE_NOT_AUTHORIZED",
        )

    async def test_windows_preserve_execution_state_and_rebuild_from_journal(self):
        gateway = MemoryArtifactGateway()
        projector = ModelContextProjector(gateway=gateway)
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("b")
        await runtime.receive_user_message("b", "original", delivery_id="first")
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        surface.attach(runtime)
        await surface.load("b", "demo")
        schemas = surface.schemas("b")
        context = CompactContext("b", await runtime.snapshot("b"), projector, None)
        boundary = CompactBoundary(runtime, None, context, None, None, None)
        initial = await runtime.snapshot("b")
        for index in range(2):
            material = save_document(
                gateway,
                "b",
                {"messages": [{"role": "user", "content": f"handoff {index}"}]},
            )
            args = {
                "handoff": {"artifact": material, "request": material},
                "window": {
                    "id": str(index),
                    "parent": None if index == 0 else str(index - 1),
                    "upto": 1,
                    "cutover": len(await runtime.snapshot("b")),
                    "context": material,
                    "bundle": material,
                    "recent_tail_start": 1,
                },
            }
            await boundary.publish(args)
            before = await runtime.snapshot("b")
            await boundary.publish(args)
            self.assertEqual(await runtime.snapshot("b"), before)
            self.assertEqual(surface.schemas("b"), schemas)
        events = await runtime.snapshot("b")
        self.assertEqual(events[: len(initial)], initial)
        restored = CompactContext("b", events, projector, None)
        self.assertEqual(restored.window["id"], "1")
        self.assertEqual(restored.prefix[0]["content"], "handoff 1")
        restored.runtime = runtime
        evidence = await restored.read(
            None,
            {
                "source": "b",
                "kind": "artifact",
                "reference": material,
                "offset": 0,
                "limit": 1000,
            },
        )
        self.assertIn("handoff 1", evidence["data"]["content"])

        def restored_visible(events):
            whole = StateProjector().project_visible("b", events)
            return restored.visible(events, whole).visible_event_ids

        self.assertEqual(restored_visible(events), ())
        await runtime.receive_user_message("b", "new", delivery_id="new")
        events = await runtime.snapshot("b")
        self.assertEqual(restored_visible(events), (events[-1].event_id,))
        with self.assertRaisesRegex(ValueError, "stale"):
            await boundary.publish(
                {**args, "window": {**args["window"], "id": "stale", "parent": None}}
            )
