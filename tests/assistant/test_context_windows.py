from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PIL import Image

from helperme.assistant.artifacts import MemoryArtifactGateway
from helperme.assistant.attachments import AttachmentGateway
from helperme.assistant.compact.core import (
    CompactBoundary,
    CompactContext,
    TASK,
    frozen_bundle,
    save_document,
)
from helperme.assistant.context.projection import ModelContextProjector
from helperme.runtime import AgentRuntime, MemoryJournal, StateProjector
from helperme.assistant.toolsets import ToolSurface
from tests.assistant.test_toolsets import FakeEchoProvider


def _png(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


class WindowTest(unittest.IsolatedAsyncioTestCase):
    async def test_frozen_bundle_keeps_user_image_blocks(self):
        with TemporaryDirectory() as directory:
            attachments = AttachmentGateway(Path(directory))
            store = attachments.for_session("b")
            buffer = BytesIO()
            Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
            ref = store.save_image(buffer.getvalue(), "image/png")
            projector = ModelContextProjector(
                gateway=MemoryArtifactGateway(),
                attachments=attachments,
            )
            runtime = AgentRuntime(MemoryJournal(), None, {})
            await runtime.create_session("b")
            await runtime.receive_user_message(
                "b",
                "[Image #1] look",
                delivery_id="u",
                artifact_refs=(ref.attachment_id,),
            )
            events = await runtime.snapshot("b")
            context = CompactContext("b", events, projector, None)
            bundle = frozen_bundle(projector, events, "b", context)
            user = next(
                message
                for messages in bundle["raw"].values()
                for message in messages
                if message["role"] == "user"
            )
            self.assertEqual(user["content"][0]["text"], "[Image #1] look")
            self.assertEqual(user["content"][1]["id"], ref.attachment_id)
            self.assertEqual(user["content"][1]["type"], "image")

    async def test_reader_reads_frozen_prefix_attachments_from_source(self):
        with TemporaryDirectory() as directory:
            attachments = AttachmentGateway(Path(directory))
            source_store = attachments.for_session("b")
            own_store = attachments.for_session("h")
            source_ref = source_store.save_image(_png("red"), "image/png")
            own_ref = own_store.save_image(_png("blue"), "image/png")
            gateway = MemoryArtifactGateway()
            inherited = save_document(
                gateway,
                "b",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "look"},
                                {
                                    "type": "file",
                                    "id": source_ref.attachment_id,
                                    "mime": "image/png",
                                },
                            ],
                        }
                    ],
                    "tools": [],
                    "recent": [],
                },
            )
            bundle = save_document(
                gateway,
                "b",
                {"records": [], "raw": {}, "artifacts": []},
            )
            projector = ModelContextProjector(
                gateway=gateway,
                attachments=attachments,
            )
            runtime = AgentRuntime(MemoryJournal(), None, {})
            await runtime.create_session("h")
            await runtime.receive_domain_fact(
                "h",
                TASK,
                {
                    "source": "b",
                    "bundle": bundle,
                    "inherited": inherited,
                    "upto": 1,
                    "window": None,
                },
                source="compact",
                delivery_id="task",
            )
            context = CompactContext(
                "h", await runtime.snapshot("h"), projector, None
            )
            self.assertEqual(
                context.read_attachment(source_ref.attachment_id),
                source_store.read(source_ref.attachment_id),
            )
            self.assertFalse(own_store.path(source_ref.attachment_id).is_file())
            self.assertEqual(
                context.read_attachment(own_ref.attachment_id),
                own_store.read(own_ref.attachment_id),
            )

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
