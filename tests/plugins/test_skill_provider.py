import tempfile
import unittest
from pathlib import Path

from core.agent_workspace import AgentWorkspace
from core.tools_runtime.progressive_skills import SkillLoadError
from plugins.skills.application import SkillApplicationService
from tests.plugins.test_skill_package import write_skill


class InstalledSkillProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = AgentWorkspace(root / ".helperme")
        self.workspace.initialize()
        source = root / "source"
        write_skill(
            source,
            name="demo",
            description="Demo workflow",
            body="\nFollow the demo workflow.\n",
        )
        reference = source / "references" / "guide.md"
        reference.parent.mkdir()
        reference.write_text("abcdefghij", encoding="utf-8")
        self.service = SkillApplicationService(self.workspace)
        await self.service.install_local(source)
        self.record = await self.service.set_enabled("demo", True)
        self.provider = self.service.skill_provider

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_descriptor_load_and_paginated_resource_read(self):
        self.assertEqual(
            [(item.name, item.revision) for item in self.provider.descriptors()],
            [("demo", self.record.revision)],
        )

        loaded = await self.provider.load_skill("demo", self.record.revision)
        first = await self.provider.read_resource(
            "demo",
            self.record.revision,
            "references/guide.md",
            0,
            4,
        )
        second = await self.provider.read_resource(
            "demo",
            self.record.revision,
            "references/guide.md",
            first["data"]["next_offset"],
            20,
        )

        self.assertEqual(loaded.main_instructions, "\nFollow the demo workflow.\n")
        self.assertEqual(Path(loaded.skill_dir).name, "demo")
        self.assertEqual(first["data"]["content"], "abcd")
        self.assertEqual(first["data"]["next_offset"], 4)
        self.assertEqual(second["data"]["content"], "efghij")
        self.assertIsNone(second["data"]["next_offset"])

    async def test_invalid_path_and_missing_resource_are_recoverable(self):
        with self.assertRaises(SkillLoadError) as invalid:
            await self.provider.read_resource(
                "demo",
                self.record.revision,
                "../outside",
                0,
                10,
            )
        self.assertEqual(invalid.exception.code, "INVALID_SKILL_RESOURCE_PATH")

        with self.assertRaises(SkillLoadError) as missing:
            await self.provider.read_resource(
                "demo",
                self.record.revision,
                "references/missing.md",
                0,
                10,
            )
        self.assertEqual(missing.exception.code, "SKILL_RESOURCE_NOT_FOUND")

    async def test_tampered_installed_package_is_internal_contract_failure(self):
        installed = self.workspace.skills_root / "packages" / "demo" / "SKILL.md"
        installed.write_text(
            "---\nname: demo\ndescription: Demo workflow\n---\ntampered\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "hash"):
            await self.provider.load_skill("demo", self.record.revision)

    async def test_disabled_skill_rejects_old_session_revision(self):
        await self.service.set_enabled("demo", False)

        with self.assertRaises(SkillLoadError) as captured:
            await self.provider.load_skill("demo", self.record.revision)

        self.assertEqual(captured.exception.code, "SKILL_SNAPSHOT_STALE")


if __name__ == "__main__":
    unittest.main()
