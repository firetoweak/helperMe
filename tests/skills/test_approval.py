import tempfile
import unittest
from pathlib import Path

from helperme.tools.control import ControlApprovalRequest
from helperme.paths import HelperMeHome
from helperme.skills.application import SkillApplicationService
from helperme.skills.approval import (
    SkillEnableApprovalHandler,
    SkillEnableProposalInput,
    SkillInstallApprovalHandler,
    SkillInstallProposalInput,
    SkillRepairApprovalHandler,
    SkillRepairProposalInput,
    SkillUpdateApprovalHandler,
    SkillUpdateProposalInput,
    create_skill_enable_proposal_spec,
    create_skill_install_proposal_spec,
    create_skill_repair_proposal_spec,
    create_skill_update_proposal_spec,
)
from tests.skills.test_package import write_skill


class SkillInstallApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def test_update_proposal_and_approval_use_frozen_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo", body="v1\n")
            service = SkillApplicationService(workspace)
            await service.install_local(source)
            write_skill(source, name="demo", body="v2\n")

            request = await create_skill_update_proposal_spec(service).handler(
                SkillUpdateProposalInput(skill_id="demo")
            )
            execution = await SkillUpdateApprovalHandler(service).execute(
                request.payload
            )

            self.assertTrue(execution.succeeded)
            installed = workspace.skills_root / "packages" / "demo" / "SKILL.md"
            self.assertIn("v2", installed.read_text(encoding="utf-8"))

    async def test_repair_proposal_restores_corrupted_registered_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo", body="original\n")
            service = SkillApplicationService(workspace)
            await service.install_local(source)
            installed = workspace.skills_root / "packages" / "demo" / "SKILL.md"
            installed.write_text("broken", encoding="utf-8")

            request = await create_skill_repair_proposal_spec(service).handler(
                SkillRepairProposalInput(skill_id="demo")
            )
            execution = await SkillRepairApprovalHandler(service).execute(
                request.payload
            )

            self.assertTrue(execution.succeeded)
            self.assertIn("original", installed.read_text(encoding="utf-8"))

    async def test_already_installed_is_a_deterministic_proposal_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(workspace)
            await service.install_local(source)

            result = await create_skill_install_proposal_spec(service).handler(
                SkillInstallProposalInput(
                    source_kind="local",
                    locator=str(source),
                )
            )

            self.assertEqual(result["code"], "SKILL_ALREADY_INSTALLED")

    async def test_same_content_from_new_source_keeps_current_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            first_source = root / "first-source"
            current_source = root / "current-source"
            write_skill(first_source, name="demo", body="same body\n")
            write_skill(current_source, name="demo", body="same body\n")
            service = SkillApplicationService(workspace)
            spec = create_skill_install_proposal_spec(service)

            first = await spec.handler(SkillInstallProposalInput(
                source_kind="local",
                locator=str(first_source),
            ))
            current = await spec.handler(SkillInstallProposalInput(
                source_kind="local",
                locator=str(current_source),
            ))

            self.assertIsInstance(first, ControlApprovalRequest)
            self.assertIsInstance(current, ControlApprovalRequest)
            self.assertEqual(
                first.payload["content_hash"],
                current.payload["content_hash"],
            )
            self.assertEqual(
                current.payload["source"]["locator"],
                str(current_source),
            )

            await SkillInstallApprovalHandler(service).execute(current.payload)

            record = await service.registry.get("demo")
            self.assertEqual(record.source.locator, str(current_source))

    async def test_proposal_freezes_candidate_and_approval_installs_exact_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
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
            self.assertIsInstance(request, ControlApprovalRequest)
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
            workspace = HelperMeHome(Path(directory) / ".helperme")
            service = SkillApplicationService(workspace)
            spec = create_skill_install_proposal_spec(service)

            self.assertTrue(spec.control_boundary)
            self.assertTrue(spec.exclusive_batch)

    async def test_enable_proposal_freezes_revision_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
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
