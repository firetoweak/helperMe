from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "tools" / "review_draft.py"
)
SPEC = importlib.util.spec_from_file_location("review_draft", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"cannot load review script: {SCRIPT_PATH}")
review_draft = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = review_draft
SPEC.loader.exec_module(review_draft)


class ReviewDraftTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Review Test")
        self.git("config", "user.email", "review@example.test")
        principle = self.repository / "docs" / "原则.md"
        principle.parent.mkdir()
        principle.write_text("# 原则\n\n1. Runtime 不做语义判断。\n", encoding="utf-8")
        self.git("add", "docs/原则.md")
        self.git("commit", "-m", "principles")
        self.git("commit", "--allow-empty", "-m", "design: narrow review")
        self.design_revision = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def commit_implementation(self):
        (self.repository / "implementation.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        self.git("add", "implementation.py")
        self.git("commit", "-m", "implementation")

    def test_collects_frozen_principles_design_and_head(self):
        self.commit_implementation()

        evidence = review_draft.collect_evidence(
            self.repository,
            self.design_revision,
        )

        self.assertEqual(evidence.design_revision, self.design_revision)
        self.assertEqual(evidence.head_revision, self.git("rev-parse", "HEAD").stdout.strip())
        self.assertIn("Runtime 不做语义判断", evidence.principles)
        self.assertEqual(evidence.design.strip(), "design: narrow review")

    def test_rejects_unknown_design_revision(self):
        with self.assertRaisesRegex(
            review_draft.ReviewInputError,
            "invalid design_revision",
        ):
            review_draft.collect_evidence(self.repository, "missing")

    def test_rejects_dirty_working_tree(self):
        (self.repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(
            review_draft.ReviewInputError,
            "working tree must be clean",
        ):
            review_draft.collect_evidence(
                self.repository,
                self.design_revision,
            )

    def test_rejects_design_revision_without_frozen_principles(self):
        self.git("rm", "docs/原则.md")
        self.git("commit", "-m", "remove principles")
        revision = self.git("rev-parse", "HEAD").stdout.strip()

        with self.assertRaisesRegex(
            review_draft.ReviewInputError,
            "does not exist at design_revision",
        ):
            review_draft.collect_evidence(self.repository, revision)

    def test_rejects_principle_change_after_design_revision(self):
        principle = self.repository / "docs" / "原则.md"
        principle.write_text("# changed\n", encoding="utf-8")
        self.git("add", "docs/原则.md")
        self.git("commit", "-m", "change principles")

        with self.assertRaisesRegex(
            review_draft.ReviewInputError,
            "principle_boundary_violation",
        ):
            review_draft.collect_evidence(
                self.repository,
                self.design_revision,
            )

    def test_rejects_revision_from_unrelated_history(self):
        self.git("checkout", "--orphan", "unrelated")
        self.git("rm", "-rf", ".")
        (self.repository / "other.txt").write_text("other\n", encoding="utf-8")
        self.git("add", "other.txt")
        self.git("commit", "-m", "unrelated")

        with self.assertRaisesRegex(
            review_draft.ReviewInputError,
            "not an ancestor",
        ):
            review_draft.collect_evidence(
                self.repository,
                self.design_revision,
            )

    def test_build_prompt_includes_only_frozen_review_inputs(self):
        self.commit_implementation()
        evidence = review_draft.collect_evidence(
            self.repository,
            self.design_revision,
        )

        prompt = review_draft.build_prompt("review rules", evidence)

        self.assertIn("review rules", prompt)
        self.assertIn(f"design_revision: {self.design_revision}", prompt)
        self.assertIn(f"head_revision: {evidence.head_revision}", prompt)
        self.assertIn("Runtime 不做语义判断", prompt)
        self.assertIn("design: narrow review", prompt)

    def test_run_review_passes_prompt_to_codex_and_returns_raw_exit_code(self):
        self.commit_implementation()
        evidence = review_draft.collect_evidence(
            self.repository,
            self.design_revision,
        )
        call: dict[str, object] = {}

        def runner(args, **kwargs):
            call["args"] = args
            call["kwargs"] = kwargs
            return subprocess.CompletedProcess(args, 7)

        exit_code = review_draft.run_review(
            evidence,
            "frozen prompt",
            codex_command="codex-test",
            runner=runner,
        )

        self.assertEqual(exit_code, 7)
        self.assertEqual(
            call["args"],
            [
                "codex-test",
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--cd",
                str(self.repository),
                "-",
            ],
        )
        kwargs = call["kwargs"]
        self.assertEqual(kwargs["cwd"], self.repository)
        self.assertEqual(kwargs["input"], "frozen prompt")
        self.assertTrue(kwargs["text"])


if __name__ == "__main__":
    unittest.main()
