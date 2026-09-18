from types import SimpleNamespace
import unittest

from helperme.assistant.catalog import sync_catalog, CATALOG
from helperme.assistant.context.projection import project_chat_messages
from helperme.assistant.toolsets import ToolsetDescriptor
from helperme.runtime import (
    AgentRuntime,
    MemoryJournal,
    DomainFactCommitted,
    StateProjector,
)


class CatalogTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_updates_are_append_only_and_restore_without_repetition(self):
        runtime = AgentRuntime(MemoryJournal(), None, {})
        await runtime.create_session("s")
        records = [
            SimpleNamespace(name="demo", description="first", revision=1, enabled=True)
        ]
        descriptors = [ToolsetDescriptor("mcp:z", "Z"), ToolsetDescriptor("mcp:a", "A")]
        surface = SimpleNamespace(descriptors=lambda: tuple(descriptors))
        skills = SimpleNamespace(
            catalog=lambda: [
                {"id": r.name, "description": r.description, "revision": r.revision}
                for r in records
            ]
        )
        clis = SimpleNamespace(catalog=lambda: [])
        management = SimpleNamespace(catalog_instruction=lambda sid: "mcp / skill")
        await sync_catalog(runtime, "s", surface, skills, clis, management)
        initial = await runtime.snapshot("s")
        descriptors.reverse()
        await sync_catalog(runtime, "s", surface, skills, clis, management)
        self.assertEqual(await runtime.snapshot("s"), initial)
        records[0].description = "updated"
        records[0].revision = 2
        await sync_catalog(runtime, "s", surface, skills, clis, management)
        updated = await runtime.snapshot("s")
        self.assertEqual(updated[: len(initial)], initial)
        self.assertEqual(len(updated), 2)
        self.assertTrue(
            all(
                isinstance(e.payload, DomainFactCommitted)
                and e.payload.fact_type == CATALOG
                for e in updated
            )
        )
        projector = StateProjector()
        before = project_chat_messages(initial, projector.project_visible("s", initial))
        after = project_chat_messages(updated, projector.project_visible("s", updated))
        self.assertEqual(after[: len(before)], before)
        self.assertIn("<capability_catalog>", after[-1]["content"])
        await sync_catalog(runtime, "s", surface, skills, clis, management)
        self.assertEqual(await runtime.snapshot("s"), updated)
