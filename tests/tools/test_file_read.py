from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from helperme.sandbox.api import EnvironmentBinding, ExecutionAttachment
from helperme.sandbox.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from helperme.tools.builtin.file_read import (
    GlobInput,
    GrepInput,
    _glob_relative_entries,
    _rg_scope_args,
    _rg_scope_hint,
    create_file_read_specs,
)


def _binding(root: Path) -> EnvironmentBinding:
    view = WorkspaceViewSnapshot((
        RootBinding("project", WorkspaceScope.TASK, root),
    ))
    return EnvironmentBinding(
        environment_id="local-test",
        workspace_view=view,
        permission_binding=PermissionBinding((
            ("project", FilesystemPermission.READ_WRITE),
        )),
        cwd=root,
        shell_name="powershell",
        shell_path="pwsh.exe",
        execution_attachment=ExecutionAttachment("local-test", object()),
    )


def _handlers(root: Path):
    specs = {spec.name: spec for spec in create_file_read_specs(_binding(root))}
    return specs


class RgScopeContractTest(unittest.TestCase):
    def test_default_scope_leaves_rg_filters_in_place(self):
        root = Path("/tmp/project")
        self.assertEqual(
            _rg_scope_args(root, include_hidden=False, include_ignored=False),
            [],
        )

    def test_include_hidden_still_prunes_git_unless_searching_inside_it(self):
        root = Path("/tmp/project")
        self.assertEqual(
            _rg_scope_args(root, include_hidden=True, include_ignored=False),
            ["--hidden", "--glob", "!.git/"],
        )
        self.assertEqual(
            _rg_scope_args(
                root / ".git",
                include_hidden=True,
                include_ignored=False,
            ),
            ["--hidden"],
        )

    def test_include_ignored_disables_gitignore(self):
        self.assertEqual(
            _rg_scope_args(
                Path("/tmp/project"),
                include_hidden=False,
                include_ignored=True,
            ),
            ["--no-ignore"],
        )

    def test_hint_names_skipped_filters_and_escape_hatches(self):
        hint = _rg_scope_hint(
            Path("/tmp/project"),
            include_hidden=False,
            include_ignored=False,
        )
        assert hint is not None
        self.assertIn("隐藏文件/目录", hint)
        self.assertIn("gitignore", hint)
        self.assertIn("include_hidden=true", hint)
        self.assertIn("include_ignored=true", hint)

    def test_descriptions_explain_hidden_and_gitignore_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            specs = _handlers(Path(directory))
        for name in ("glob", "grep"):
            text = specs[name].description
            self.assertIn("隐藏", text)
            self.assertIn("gitignore", text)
            self.assertIn("include_hidden", text)
            self.assertIn("include_ignored", text)

    def test_relative_entries_keep_parent_dirs_within_max_depth(self):
        root = Path("/tmp/project")
        entries = _glob_relative_entries(
            [root / "src" / "nested" / "a.py"],
            root,
            1,
        )
        self.assertEqual(entries, [("src", "dir")])


class FileSearchIgnoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        subprocess.run(
            ["git", "init"],
            cwd=self.root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        (self.root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "keep.py").write_text("keep\n", encoding="utf-8")
        (self.root / "ignored").mkdir()
        (self.root / "ignored" / "secret.py").write_text(
            "secret-token\n",
            encoding="utf-8",
        )
        (self.root / ".hidden.txt").write_text("hidden-token\n", encoding="utf-8")
        (self.root / ".github").mkdir()
        (self.root / ".github" / "README.md").write_text(
            "workflow\n",
            encoding="utf-8",
        )
        git_object = self.root / ".git" / "objects" / "pack"
        git_object.mkdir(parents=True, exist_ok=True)
        (git_object / "pack-token").write_text("git-object\n", encoding="utf-8")
        specs = _handlers(self.root)
        self.glob = specs["glob"].handler
        self.grep = specs["grep"].handler

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _match_paths(self, result: dict) -> set[str]:
        return {item["path"] for item in result["matches"]}

    async def test_glob_skips_hidden_and_gitignore_by_default(self):
        result = await self.glob(GlobInput(pattern="*", max_results=100))

        self.assertTrue(result["ok"])
        paths = self._match_paths(result)
        self.assertIn("src/keep.py", paths)
        self.assertNotIn(".hidden.txt", paths)
        self.assertNotIn(".github/README.md", paths)
        self.assertNotIn("ignored/secret.py", paths)
        self.assertFalse(any(".git" in path for path in paths))
        self.assertIn("隐藏文件/目录", result["hint"])
        self.assertIn("gitignore", result["hint"])

    async def test_glob_include_hidden_finds_dot_files_but_not_git(self):
        result = await self.glob(
            GlobInput(pattern="*", include_hidden=True, max_results=100),
        )

        self.assertTrue(result["ok"])
        paths = self._match_paths(result)
        self.assertIn(".hidden.txt", paths)
        self.assertIn(".github/README.md", paths)
        self.assertFalse(any(path.startswith(".git/") for path in paths))

    async def test_glob_include_ignored_finds_gitignored_files(self):
        result = await self.glob(
            GlobInput(pattern="*.py", include_ignored=True, max_results=100),
        )

        self.assertTrue(result["ok"])
        paths = self._match_paths(result)
        self.assertIn("ignored/secret.py", paths)
        self.assertIn("src/keep.py", paths)

    async def test_glob_explicit_path_enters_hidden_directory(self):
        result = await self.glob(
            GlobInput(pattern="*", path=".github", max_results=100),
        )

        self.assertTrue(result["ok"])
        self.assertIn(".github/README.md", self._match_paths(result))

    async def test_grep_skips_hidden_and_gitignore_by_default(self):
        result = await self.grep(GrepInput(query="token", max_results=100))

        self.assertTrue(result["ok"])
        files = {hit["file"] for hit in result["hits"]}
        self.assertEqual(files, set())

        visible = await self.grep(GrepInput(query="keep", max_results=100))
        self.assertEqual(
            {hit["file"] for hit in visible["hits"]},
            {"src/keep.py"},
        )

    async def test_grep_include_hidden_and_ignored_find_skipped_files(self):
        hidden = await self.grep(
            GrepInput(query="hidden-token", include_hidden=True),
        )
        ignored = await self.grep(
            GrepInput(query="secret-token", include_ignored=True),
        )

        self.assertEqual(
            {hit["file"] for hit in hidden["hits"]},
            {".hidden.txt"},
        )
        self.assertEqual(
            {hit["file"] for hit in ignored["hits"]},
            {"ignored/secret.py"},
        )


if __name__ == "__main__":
    unittest.main()
