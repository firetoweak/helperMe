import tempfile
import unittest
from pathlib import Path

from helperme.paths import HelperMeHome
from helperme.tools.spec import EmptyInput
from helperme.skills.application import SkillApplicationService
from helperme.skills.management_tools import SkillIdInput, create_skill_management_specs
from tests.skills.test_package import write_skill


class SkillManagementToolsTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_skill_remains_observable_testable_and_diagnosable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = HelperMeHome(root / ".helperme")
            workspace.initialize()
            source = root / "source"
            write_skill(source, name="demo")
            service = SkillApplicationService(workspace)
            await service.install_local(source)
            specs = {
                spec.name: spec
                for spec in create_skill_management_specs(service)
            }

            listed = await specs["list_installed_skills"].handler(EmptyInput())
            inspected = await specs["inspect_installed_skill"].handler(
                SkillIdInput(skill_id="demo")
            )
            tested = await specs["test_installed_skill"].handler(
                SkillIdInput(skill_id="demo")
            )

            self.assertFalse(listed["data"]["skills"][0]["enabled"])
            self.assertEqual(inspected["code"], "SKILL_INSPECTED")
            self.assertEqual(tested["code"], "SKILL_TEST_PASSED")
            self.assertFalse(tested["data"]["enabled"])
