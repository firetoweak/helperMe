import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.get_changes import GetChangesInput, create_get_changes_specs
from tools.workspace import WorkspaceSandbox, WorkspaceSandboxes


class GetChangesEarlyFailTest(unittest.TestCase):
    @patch("tools.get_changes.subprocess.run")
    def test_non_git_workspace_reports_verification_failure(self, run):
        run.return_value = Mock(returncode=128)
        with tempfile.TemporaryDirectory() as directory:
            workspaces = WorkspaceSandboxes({
                "project": WorkspaceSandbox(Path(directory))
            })
            get_changes = create_get_changes_specs(workspaces)[0].handler

            result = get_changes(GetChangesInput(root="project"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VERIFICATION_BACKEND_UNAVAILABLE")
        self.assertIsNone(result["changed"])


if __name__ == "__main__":
    unittest.main()
