from __future__ import annotations

from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
ATTRIBUTES_PATH = ROOT / ".gitattributes"
MARKDOWN_SAMPLES = (
    "AGENTS.md",
    "docs/README.md",
    "docs/原则.md",
)
PYTHON_SAMPLES = (
    "helperme/runtime/__init__.py",
    "tests/architecture/test_gitattributes.py",
)


def _check_attr(attribute: str, path: str) -> str:
    result = subprocess.run(
        ["git", "check-attr", attribute, "--", path],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    line = result.stdout.strip()
    _, _, value = line.rpartition(": ")
    return value


class GitAttributesTest(unittest.TestCase):
    def test_repository_locks_text_to_lf(self):
        text = ATTRIBUTES_PATH.read_text(encoding="utf-8")

        self.assertTrue(ATTRIBUTES_PATH.is_file())
        self.assertIn("text=auto", text)
        self.assertIn("eol=lf", text)

    def test_git_applies_lf_to_markdown_and_python(self):
        for path in (*MARKDOWN_SAMPLES, *PYTHON_SAMPLES):
            with self.subTest(path=path):
                self.assertEqual(_check_attr("eol", path), "lf")
                self.assertIn(_check_attr("text", path), {"auto", "set"})
