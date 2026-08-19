import os
import tempfile
import unittest
from pathlib import Path

from plugins.skills.models import SkillPackageLimits
from plugins.skills.package import (
    LocalSkillPackageReader,
    SkillPackageError,
    validate_relative_skill_path,
)


def write_skill(
    root: Path,
    *,
    name: str = "python-testing",
    description: str = "指导 Python 测试",
    body: str = "\n# Workflow\n\nRun tests.\n",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
        newline="\n",
    )


class LocalSkillPackageReaderTest(unittest.TestCase):
    def test_reads_frontmatter_body_files_and_deterministic_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source-name-may-differ"
            write_skill(root)
            reference = root / "references" / "pytest.md"
            reference.parent.mkdir()
            reference.write_text("reference", encoding="utf-8")

            first = LocalSkillPackageReader().read(root)
            second = LocalSkillPackageReader().read(root)

            self.assertEqual(first.name, "python-testing")
            self.assertEqual(first.description, "指导 Python 测试")
            self.assertEqual(first.main_instructions, "\n# Workflow\n\nRun tests.\n")
            self.assertEqual(
                [item.relative_path for item in first.files],
                ["SKILL.md", "references/pytest.md"],
            )
            self.assertEqual(first.content_hash, second.content_hash)

            reference.write_text("changed", encoding="utf-8")
            changed = LocalSkillPackageReader().read(root)
            self.assertNotEqual(first.content_hash, changed.content_hash)

    def test_rejects_missing_invalid_and_duplicate_frontmatter(self):
        cases = {
            "missing": "# no frontmatter\n",
            "unterminated": "---\nname: demo\ndescription: demo\n",
            "not_object": "---\n- demo\n---\nbody\n",
            "duplicate": (
                "---\nname: demo\nname: other\ndescription: demo\n---\nbody\n"
            ),
            "invalid_name": (
                "---\nname: Demo Skill\ndescription: demo\n---\nbody\n"
            ),
            "empty_description": (
                "---\nname: demo\ndescription: ''\n---\nbody\n"
            ),
            "multiline_description": (
                "---\nname: demo\ndescription: |\n  line one\n  line two\n"
                "---\nbody\n"
            ),
        }
        for label, content in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "SKILL.md").write_text(content, encoding="utf-8")
                with self.assertRaises(SkillPackageError):
                    LocalSkillPackageReader().read(root)

    def test_rejects_file_count_file_size_total_size_and_main_instruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(root, body="12345")
            (root / "extra.txt").write_text("1234", encoding="utf-8")

            configurations = (
                SkillPackageLimits(max_files=1),
                SkillPackageLimits(max_file_bytes=3),
                SkillPackageLimits(max_total_bytes=5),
                SkillPackageLimits(max_main_instruction_chars=4),
            )
            for limits in configurations:
                with self.subTest(limits=limits):
                    with self.assertRaises(SkillPackageError):
                        LocalSkillPackageReader(limits).read(root)

    def test_rejects_symlink_when_platform_allows_creating_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skill"
            write_skill(root)
            target = Path(directory) / "outside.txt"
            target.write_text("outside", encoding="utf-8")
            link = root / "references.txt"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("current platform cannot create symlinks")

            with self.assertRaisesRegex(SkillPackageError, "symlink"):
                LocalSkillPackageReader().read(root)

    def test_rejects_non_relative_or_non_normalized_bundle_paths(self):
        for path in (
            "../outside",
            "/absolute",
            "references\\windows.md",
            "references/../outside",
            "./SKILL.md",
        ):
            with self.subTest(path=path):
                with self.assertRaises(SkillPackageError):
                    validate_relative_skill_path(path)


if __name__ == "__main__":
    unittest.main()
