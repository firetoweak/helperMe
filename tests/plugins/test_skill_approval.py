import tempfile
import unittest
from pathlib import Path

from core.approval import ApprovalRequest
from core.agent_workspace import AgentWorkspace
from plugins.skills.application import SkillApplicationService
from plugins.skills.approval import (
    SkillEnableApprovalHandler,
    SkillEnableProposalInput,
    SkillInstallApprovalHandler,
    SkillInstallProposalInput,
    create_skill_enable_proposal_spec,
    create_skill_install_proposal_spec,
)
from tests.plugins.test_skill_package import write_skill


class SkillInstallApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def test_proposal_freezes_candidate_and_approval_installs_exact_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = AgentWorkspace(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(
                source,
                name="demo",
                description="Frozen v1",
                body="frozen body\n",
            )
            service = SkillApplicationService(workspace)
            spec = create_skill_install_proposal_spec(service)

            request = await spec.handler(SkillInstallProposalInput(
                source_kind="local",
                locator=str(source),
            ))
            self.assertIsInstance(request, ApprovalRequest)
            frozen_hash = request.payload["content_hash"]

            write_skill(
                source,
                name="demo",
                description="Drifted v2",
                body="drifted body\n",
            )
            execution = await SkillInstallApprovalHandler(service).execute(
                request.payload
            )

            record = await service.registry.get("demo")
            installed = service.skills_root / "packages" / "demo" / "SKILL.md"
            text = installed.read_text(encoding="utf-8")
            self.assertTrue(execution.succeeded)
            self.assertFalse(record.enabled)
            self.assertEqual(record.content_hash, frozen_hash)
            self.assertIn("Frozen v1", text)
            self.assertNotIn("Drifted v2", text)

    async def test_install_proposal_is_both_approval_boundary_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            service = SkillApplicationService(workspace)
            spec = create_skill_install_proposal_spec(service)

            self.assertTrue(spec.control_boundary)
            self.assertTrue(spec.exclusive_batch)

    async def test_enable_proposal_freezes_revision_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = AgentWorkspace(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(workspace)
            await service.install_local(source)
            spec = create_skill_enable_proposal_spec(service)
            request = await spec.handler(SkillEnableProposalInput(skill_id="demo"))

            await service.set_enabled("demo", True)
            await service.set_enabled("demo", False)

            with self.assertRaisesRegex(ValueError, "过期"):
                await SkillEnableApprovalHandler(service).execute(request.payload)

            self.assertTrue(spec.control_boundary)
            self.assertTrue(spec.exclusive_batch)


if __name__ == "__main__":
    unittest.main()
    create_skill_enable_proposal_spec,
