import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.agent_workspace import AgentWorkspace


class AgentWorkspaceTest(unittest.TestCase):
    def test_default_root_is_hidden_helperme_in_user_home(self):
        with patch("core.agent_workspace.Path.home", return_value=Path("C:/Users/test")):
            workspace = AgentWorkspace.default()

        self.assertEqual(workspace.root, Path("C:/Users/test/.helperme").resolve())

    def test_layout_contains_only_core_owned_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")

            workspace.initialize()

            self.assertTrue(workspace.sessions_root.is_dir())
            self.assertTrue(workspace.plugins_root.is_dir())
            self.assertTrue(workspace.state_root.is_dir())
            self.assertEqual(workspace.sessions_root.parent, workspace.root)
            self.assertEqual(workspace.plugins_root.parent, workspace.root)
            self.assertEqual(workspace.state_root.parent, workspace.root)


if __name__ == "__main__":
    unittest.main()
