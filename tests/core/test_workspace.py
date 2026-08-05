import tempfile
import unittest
from pathlib import Path

from tools.workspace import (
    AbsolutePathNotAllowed,
    PathOutsideWorkspace,
    UnknownWorkspaceRoot,
    WorkspaceSandbox,
    WorkspaceSandboxes,
)


class WorkspaceSandboxTest(unittest.TestCase):
    def test_resolves_relative_path_inside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sandbox = WorkspaceSandbox(root)

            resolved = sandbox.resolve("docs/../new.txt")

            self.assertEqual(resolved, root.resolve() / "new.txt")

    def test_nonexistent_path_is_still_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = WorkspaceSandbox(Path(directory))

            resolved = sandbox.resolve("new/not-exist.txt")

            self.assertFalse(resolved.exists())

    def test_rejects_absolute_path_even_when_it_is_inside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = WorkspaceSandbox(Path(directory))
            inside = Path(directory) / "inside.txt"

            with self.assertRaises(AbsolutePathNotAllowed):
                sandbox.resolve(str(inside))

    def test_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = WorkspaceSandbox(Path(directory))

            with self.assertRaises(PathOutsideWorkspace):
                sandbox.resolve("../outside.txt")

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"当前环境无法创建符号链接: {exc}")
            sandbox = WorkspaceSandbox(root)

            with self.assertRaises(PathOutsideWorkspace):
                sandbox.resolve("link/new.txt")

    def test_each_named_root_has_an_independent_sandbox(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            project = WorkspaceSandbox(Path(first))
            notes = WorkspaceSandbox(Path(second))
            workspaces = WorkspaceSandboxes({"project": project, "notes": notes})

            self.assertIs(workspaces.get("project"), project)
            self.assertIs(workspaces.get("notes"), notes)
            with self.assertRaises(UnknownWorkspaceRoot):
                workspaces.get("missing")


if __name__ == "__main__":
    unittest.main()
