import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helperme.config import INITIAL_CONFIG, InitialConfigCreated, load_app_config


class AppConfigTest(unittest.TestCase):
    def _data(self, ratio: float = 0.8) -> dict:
        return {
            "model": {
                "name": "model",
                "base_url": "https://example.test/v1",
                "api_key": "key",
                "enable_thinking": True,
            },
            "workspace": {"root": ".", "full_access": False},
            "runtime": {
                "model_context_limit": 1000,
                "input_budget_ratio": ratio,
            },
            "channels": {},
        }

    def _write_config(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_example_config_matches_current_schema(self):
        path = Path(__file__).resolve().parents[2] / "config.example.json"

        config = load_app_config(path)
        document = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(document, INITIAL_CONFIG)
        self.assertIsNotNone(config.channels.telegram)

    def test_first_run_creates_default_config_and_stops(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            expected_path = home / ".helperme" / "config.json"

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("helperme.paths.Path.home", return_value=home),
                self.assertRaises(InitialConfigCreated) as raised,
            ):
                load_app_config()

            document = json.loads(expected_path.read_text(encoding="utf-8"))

        self.assertEqual(raised.exception.path, expected_path.resolve())
        self.assertEqual(document, INITIAL_CONFIG)

    def test_loads_default_config_from_helperme_home(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            config_path = home / ".helperme" / "config.json"
            config_path.parent.mkdir()
            self._write_config(config_path, self._data())

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("helperme.paths.Path.home", return_value=home),
            ):
                config = load_app_config()

        self.assertEqual(config.model.name, "model")
        self.assertTrue(config.model.enable_thinking)
        self.assertTrue(0 < config.runtime.input_budget_ratio < 1)
        self.assertIsInstance(config.workspace.root, Path)
        self.assertIsNone(config.channels.telegram)

    def test_parses_telegram_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["channels"] = {
                "telegram": {
                    "bot_token": " token ",
                    "allowed_chat_id": -7,
                }
            }
            self._write_config(path, data)

            telegram = load_app_config(path).channels.telegram

        self.assertIsNotNone(telegram)
        self.assertEqual(telegram.bot_token, "token")
        self.assertEqual(telegram.allowed_chat_id, -7)

    def test_allows_unpaired_telegram_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["channels"] = {
                "telegram": {
                    "bot_token": "token",
                    "allowed_chat_id": None,
                }
            }
            self._write_config(path, data)

            telegram = load_app_config(path).channels.telegram

        self.assertIsNotNone(telegram)
        self.assertIsNone(telegram.allowed_chat_id)

    def test_explicit_path_takes_priority_over_environment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self._write_config(path, self._data())

            with patch.dict(
                os.environ,
                {"HELPERME_CONFIG": str(path.with_name("missing.json"))},
            ):
                config = load_app_config(path)

        self.assertEqual(config.model.name, "model")

    def test_environment_overrides_default_path(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self._write_config(path, self._data())

            with patch.dict(os.environ, {"HELPERME_CONFIG": str(path)}):
                config = load_app_config()

        self.assertEqual(config.model.name, "model")

    def test_explicit_missing_path_is_not_created(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "custom" / "config.json"

            with self.assertRaises(FileNotFoundError):
                load_app_config(path)

            self.assertFalse(path.exists())

    def test_rejects_unknown_fields(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["runtime"]["unexpected"] = 8
            self._write_config(path, data)

            with self.assertRaises(ValueError):
                load_app_config(path)

    def test_rejects_non_boolean_enable_thinking(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["model"]["enable_thinking"] = 1
            self._write_config(path, data)

            with self.assertRaisesRegex(ValueError, "enable_thinking"):
                load_app_config(path)

    def test_rejects_budget_ratio_at_closed_upper_bound(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self._write_config(path, self._data(ratio=1.0))

            with self.assertRaisesRegex(ValueError, r"\(0, 1\)"):
                load_app_config(path)

    def test_rejects_incomplete_telegram_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["channels"] = {"telegram": {"bot_token": "token"}}
            self._write_config(path, data)

            with self.assertRaisesRegex(ValueError, "allowed_chat_id"):
                load_app_config(path)

    def test_rejects_null_telegram_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = self._data()
            data["channels"] = {"telegram": None}
            self._write_config(path, data)

            with self.assertRaisesRegex(ValueError, "必须是映射"):
                load_app_config(path)
