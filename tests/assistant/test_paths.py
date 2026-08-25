import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helperme.paths import HelperMeHome


class HelperMeHomeTest(unittest.TestCase):
    def test_default_root_is_hidden_helperme_in_user_home(self):
        with patch("helperme.paths.Path.home", return_value=Path("C:/Users/test")):
            home = HelperMeHome.default()

        self.assertEqual(home.root, Path("C:/Users/test/.helperme").resolve())

    def test_layout_contains_product_data_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            home = HelperMeHome(Path(directory) / ".helperme")

            home.initialize()

            self.assertTrue(home.sessions_root.is_dir())
            self.assertTrue(home.mcp_root.is_dir())
            self.assertTrue(home.skills_root.is_dir())
            self.assertTrue(home.state_root.is_dir())
            self.assertEqual(home.config_path, home.root / "config.json")
            self.assertEqual(home.sessions_root.parent, home.root)
            self.assertEqual(home.mcp_root.parent, home.root)
            self.assertEqual(home.skills_root.parent, home.root)
            self.assertEqual(home.state_root.parent, home.root)
            self.assertEqual(home.runtime_streams_root.parent, home.root)


if __name__ == "__main__":
    unittest.main()
