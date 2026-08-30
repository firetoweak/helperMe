from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
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
        self.git("switch", "-c", "codex/review-test")
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

    def test_rejects_candidate_implementation_on_main(self):
        self.git("switch", "main")

        with self.assertRaisesRegex(
            review_draft.ReviewInputError,
            "draft_review_branch_violation",
        ):
            review_draft.collect_evidence(
                self.repository,
                self.design_revision,
            )

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

    def test_project_prompt_requires_design_code_test_traceability(self):
        template = (
            Path(__file__).resolve().parents[2]
            / "tools"
            / "review_prompt.md"
        ).read_text(encoding="utf-8")

        design = template.index("提取 `design_obligations`")
        implementation = template.index("读取生产代码变化")
        tests = template.index("单独读取测试变化")
        self.assertLess(design, implementation)
        self.assertLess(implementation, tests)
        self.assertIn("测试通过不能作为设计正确的证据", template)
        self.assertIn("implementation_evidence", template)
        self.assertIn("test_evidence", template)
        self.assertIn("你没有编码 Session 的上下文", template)
        self.assertIn("即使运行环境向你展示了先前对话", template)
        self.assertIn("只读不改是软约束", template)
        self.assertIn("没有机制阻止你改文件", template)
        self.assertIn("确认当前分支不是 `main`", template)
        self.assertIn("若当前分支是 `main`，审查无效", template)

    def test_script_does_not_launch_a_reviewer(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertNotIn("codex", source)
        self.assertNotIn("shutil", source)
        self.assertFalse(hasattr(review_draft, "run_review"))

    def test_main_prints_frozen_prompt_without_launching_a_reviewer(self):
        self.commit_implementation()
        stdout = io.StringIO()
        stderr = io.StringIO()
        original_cwd = Path.cwd()
        try:
            os.chdir(self.repository)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = review_draft.main([self.design_revision])
        finally:
            os.chdir(original_cwd)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr.getvalue(), "")
        prompt = stdout.getvalue()
        self.assertIn(self.design_revision, prompt)
        self.assertIn("冻结输入", prompt)
        self.assertIn("只读不改是软约束", prompt)
        self.assertIn("design: narrow review", prompt)


if __name__ == "__main__":
    unittest.main()
