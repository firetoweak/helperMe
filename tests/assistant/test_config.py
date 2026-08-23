import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from helperme.config import load_app_config


class AppConfigTest(unittest.TestCase):
    def _write_config(self, path: Path, ratio: str) -> None:
        path.write_text(
            f"""
model:
  name: model
  base_url: https://example.test/v1
  api_key: key
workspace:
  root: .
  full_access: false
runtime:
  max_steps: 10
  model_context_limit: 1000
  input_budget_ratio: {ratio}
""".strip(),
            encoding="utf-8",
        )

    def test_load_app_config_from_repo_yaml(self):
        config = load_app_config()
        self.assertTrue(config.model.name)
        self.assertGreater(config.runtime.max_steps, 0)
        self.assertTrue(0 < config.runtime.input_budget_ratio < 1)
        self.assertIsInstance(config.workspace.root, Path)

    def test_rejects_obsolete_or_unknown_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                """
model:
  name: model
  base_url: https://example.test/v1
  api_key: key
workspace:
  root: .
  full_access: false
runtime:
  max_steps: 10
  max_goal_turns: 8
  model_context_limit: 1000
  input_budget_ratio: 0.8
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_app_config(path)

    def test_rejects_budget_ratio_at_closed_upper_bound(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            self._write_config(path, "1.0")

            with self.assertRaisesRegex(ValueError, r"\(0, 1\)"):
                load_app_config(path)
