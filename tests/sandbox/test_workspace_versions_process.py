import asyncio
from pathlib import Path
import subprocess

import pytest

from helperme.sandbox.versions import UnknownWorkspaceVersion, WorkspaceVersions


pytestmark = pytest.mark.process


def git(root: Path, *args: str):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def test_restore_is_reversible_and_preserves_user_git(tmp_path):
    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        git(root, "init")
        (root / "file.txt").write_bytes(b"original\r\n")
        git(root, "add", ".")
        git(root, "-c", "user.name=Test", "-c", "user.email=test@local",
            "commit", "-m", "user commit")
        (root / "file.txt").write_bytes(b"staged\r\n")
        git(root, "add", ".")
        user_head = git(root, "rev-parse", "HEAD")
        user_index = (root / ".git" / "index").read_bytes()
        (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (root / ".gitattributes").write_text("*.txt text eol=lf\n", encoding="utf-8")
        (root / ".git" / "info" / "exclude").write_text("local-only\n", encoding="utf-8")
        (root / "local-only").write_text("do not record")
        (root / "ignored").mkdir()
        (root / "ignored" / "keep.txt").write_text("ignored")
        versions = WorkspaceVersions(root, tmp_path / "versions")
        baseline = await versions.record()
        tracked = git(root, f"--git-dir={versions.repository}", "ls-tree", "-r", "--name-only", baseline)
        assert b"local-only" not in tracked
        assert await versions.record() == baseline
        (root / "file.txt").write_bytes(b"changed\r\n")
        (root / "new.txt").write_text("new")
        changed = await versions.record()
        (root / "manual.txt").write_text("manual")
        result = await versions.restore(baseline)
        assert (root / "file.txt").read_bytes() == b"staged\r\n"
        assert not (root / "new.txt").exists()
        assert not (root / "manual.txt").exists()
        assert result.version not in (baseline, changed, result.before_version)
        assert (root / "ignored" / "keep.txt").read_text() == "ignored"
        await versions.restore(result.before_version)
        assert (root / "manual.txt").read_text() == "manual"
        assert (root / "new.txt").read_text() == "new"
        assert (root / "file.txt").read_bytes() == b"changed\r\n"
        assert git(root, "rev-parse", "HEAD") == user_head
        assert (root / ".git" / "index").read_bytes() == user_index
    asyncio.run(scenario())


def test_plain_directory_and_nested_repository_files(tmp_path):
    async def scenario():
        root = tmp_path / "plain"
        root.mkdir()
        nested = root / "nested"
        nested.mkdir()
        git(nested, "init")
        (nested / "file").write_text("first")
        home = root / "home"
        home.mkdir()
        (home / "journal").write_text("first fact")
        versions = WorkspaceVersions(root, home / "versions", excluded_roots=(home,))
        initial = await versions.record()
        (nested / "file").write_text("second")
        (home / "journal").write_text("new fact")
        await versions.restore(initial)
        assert (nested / "file").read_text() == "first"
        assert (home / "journal").read_text() == "new fact"
        assert (nested / ".git" / "HEAD").is_file()
        with pytest.raises(UnknownWorkspaceVersion):
            await versions.restore("0" * 40)
    asyncio.run(scenario())


def test_instances_share_one_track_and_corruption_is_not_an_environment_failure(tmp_path):
    async def scenario():
        root = tmp_path / "workspace"
        root.mkdir()
        (root / "中文 space.txt").write_text("content", encoding="utf-8")
        first = WorkspaceVersions(root, tmp_path / "versions")
        second = WorkspaceVersions(root, tmp_path / "versions")
        a, b = await asyncio.gather(first.record(), second.record())
        assert a == b
        (root / "中文 space.txt").write_text("changed", encoding="utf-8")
        newer = await second.record()
        parent = git(root, f"--git-dir={first.repository}", "rev-parse", f"{newer}^")
        assert parent.decode().strip() == a
        (first.repository / "HEAD").write_text("corrupt")
        with pytest.raises(RuntimeError, match="workspace Git"):
            await first.record()
    asyncio.run(scenario())
