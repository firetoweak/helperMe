from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from typing import Mapping


class WorkspaceInputError(ValueError):
    code = "WORKSPACE_INPUT_ERROR"


class AbsolutePathNotAllowed(WorkspaceInputError):
    code = "ABSOLUTE_PATH_NOT_ALLOWED"

    def __init__(self, path: str) -> None:
        super().__init__(f"path 必须是 workspace root 内的相对路径: {path}")


class PathOutsideWorkspace(WorkspaceInputError):
    code = "PATH_OUTSIDE_WORKSPACE"

    def __init__(self, path: str) -> None:
        super().__init__(f"path 解析后越出 workspace root: {path}")


class UnknownWorkspaceRoot(WorkspaceInputError):
    code = "UNKNOWN_WORKSPACE_ROOT"

    def __init__(self, name: str) -> None:
        super().__init__(f"未知的 workspace root: {name}")


class FilesystemAccessMode(str, Enum):
    SCOPED = "scoped"
    HOST = "host"


def discover_host_filesystem_roots() -> dict[str, Path]:
    if os.name == "nt":
        roots = {
            f"drive_{letter.lower()}": Path(f"{letter}:\\")
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{letter}:\\").is_dir()
        }
    else:
        roots = {"filesystem": Path("/")}
    if not roots:
        raise RuntimeError("未发现可访问的宿主机文件系统 root")
    return roots


@dataclass(frozen=True)
class WorkspaceSandbox:
    """单个 workspace root 的路径权限边界。"""

    root: Path

    def __post_init__(self) -> None:
        resolved_root = self.root.resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"workspace root 不是已存在的目录: {resolved_root}")
        object.__setattr__(self, "root", resolved_root)

    def resolve(self, path: str) -> Path:
        relative_path = Path(path)
        if relative_path.is_absolute() or relative_path.drive:
            raise AbsolutePathNotAllowed(path)

        resolved = (self.root / relative_path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise PathOutsideWorkspace(path) from None
        return resolved

    def relative(self, path: Path | str) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()


class WorkspaceSandboxes:
    """按逻辑名称选择彼此独立的单根 Sandbox。"""

    def __init__(self, sandboxes: Mapping[str, WorkspaceSandbox]) -> None:
        if not sandboxes:
            raise ValueError("至少需要配置一个 workspace root")
        if any(not name or not name.strip() for name in sandboxes):
            raise ValueError("workspace root 名称不能为空")
        self._sandboxes = dict(sandboxes)

    def get(self, name: str) -> WorkspaceSandbox:
        try:
            return self._sandboxes[name]
        except KeyError:
            raise UnknownWorkspaceRoot(name) from None

    def info(self) -> list[dict[str, str]]:
        return [{"name": name} for name in self._sandboxes]

    def values(self) -> tuple[WorkspaceSandbox, ...]:
        return tuple(self._sandboxes.values())


def workspace_error(exc: WorkspaceInputError) -> dict[str, str | bool]:
    return {"ok": False, "code": exc.code, "error": str(exc)}
