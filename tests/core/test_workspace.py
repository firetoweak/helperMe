import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.workspace import _to_workspace_relative


class WorkspaceContractTest(unittest.TestCase):
    def test_outside_path_is_not_returned_as_absolute_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            outside = root / "outside.txt"
            workspace.mkdir()
            outside.touch()

            with patch("tools.workspace.WORKSPACE", workspace):
                with self.assertRaises(ValueError):
                    _to_workspace_relative(outside)


if __name__ == "__main__":
    unittest.main()
