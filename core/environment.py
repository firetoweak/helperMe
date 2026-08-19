from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from html import escape
import os
from pathlib import Path
from typing import Any, Protocol


class EnvironmentInputError(ValueError):
    code = "ENVIRONMENT_INPUT_ERROR"


class InvalidEnvironmentPath(EnvironmentInputError):
    code = "INVALID_ENVIRONMENT_PATH"


class PathOutsideWorkspaceView(EnvironmentInputError):
    code = "PATH_OUTSIDE_WORKSPACE_VIEW"

    def __init__(self, path: str) -> None:
        super().__init__(f"path 不属于当前 Workspace View: {path}")


class EnvironmentPermissionDenied(EnvironmentInputError):
    code = "ENVIRONMENT_PERMISSION_DENIED"

    def __init__(self, path: str, access: str) -> None:
        super().__init__(f"当前 Environment 不允许对 path 执行 {access}: {path}")


class UnknownEnvironment(EnvironmentInputError):
    code = "UNKNOWN_ENVIRONMENT"

    def __init__(self, environment_id: str) -> None:
        super().__init__(f"未知的 Environment: {environment_id}")


class UnknownWorkspaceRoot(EnvironmentInputError):
    code = "UNKNOWN_WORKSPACE_ROOT"

    def __init__(self, root_id: str) -> None:
        super().__init__(f"未知的 workspace root: {root_id}")


class WorkspaceScope(str, Enum):
    TASK = "task_workspace"
    HOST = "host_filesystem"


class FilesystemPermission(str, Enum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class FilesystemAccessMode(str, Enum):
    SCOPED = "scoped"
    HOST = "host"


@dataclass(frozen=True)
class RootBinding:
    root_id: str
    scope: WorkspaceScope
    path: Path

    def __post_init__(self) -> None:
        if not self.root_id or not self.root_id.strip():
            raise ValueError("workspace root id 不能为空")
        resolved = self.path.resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace root 不是已存在的目录: {resolved}")
        object.__setattr__(self, "path", resolved)

    def to_dict(self) -> dict[str, str]:
        return {
            "root_id": self.root_id,
            "scope": self.scope.value,
            "path": str(self.path),
        }

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "RootBinding":
        return cls(
            root_id=value["root_id"],
            scope=WorkspaceScope(value["scope"]),
            path=Path(value["path"]),
        )


@dataclass(frozen=True)
class WorkspaceMembership:
    root_id: str
    scope: WorkspaceScope
    display_path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "root_id": self.root_id,
            "scope": self.scope.value,
            "display_path": self.display_path,
        }


@dataclass(frozen=True)
class WorkspaceViewSnapshot:
    roots: tuple[RootBinding, ...]

    def __post_init__(self) -> None:
        if not self.roots:
            raise ValueError("Workspace View 至少需要一个 root")
        ids = [root.root_id for root in self.roots]
        if len(set(ids)) != len(ids):
            raise ValueError("Workspace View root id 不能重复")
        paths = [root.path for root in self.roots]
        if len(set(paths)) != len(paths):
            raise ValueError("同一物理路径不能重复注册为多个 workspace root")

    def get(self, root_id: str) -> RootBinding:
        try:
            return next(root for root in self.roots if root.root_id == root_id)
        except StopIteration:
            raise UnknownWorkspaceRoot(root_id) from None

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {"roots": [root.to_dict() for root in self.roots]}

    @classmethod
    def from_dict(
        cls,
        value: dict[str, list[dict[str, str]]],
    ) -> "WorkspaceViewSnapshot":
        return cls(tuple(
            RootBinding.from_dict(root)
            for root in value["roots"]
        ))

    def membership(self, path: Path) -> WorkspaceMembership:
        resolved = path.resolve()
        candidates = [
            root for root in self.roots if resolved.is_relative_to(root.path)
        ]
        if not candidates:
            raise PathOutsideWorkspaceView(str(path))
        # 嵌套 roots 使用最具体的 root，保证一个位置只有一个归属身份。
        root = max(candidates, key=lambda item: len(item.path.parts))
        relative = resolved.relative_to(root.path)
        return WorkspaceMembership(
            root_id=root.root_id,
            scope=root.scope,
            display_path=relative.as_posix() or ".",
        )


@dataclass(frozen=True)
class PermissionBinding:
    filesystem: tuple[tuple[str, FilesystemPermission], ...]
    network_access: str = "restricted"

    @classmethod
    def read_write(
        cls,
        view: WorkspaceViewSnapshot,
        *,
        network_access: str = "restricted",
    ) -> "PermissionBinding":
        return cls(tuple(
            (root.root_id, FilesystemPermission.READ_WRITE)
            for root in view.roots
        ), network_access=network_access)

    def access_for(self, root_id: str) -> FilesystemPermission:
        try:
            return next(
                access for candidate, access in self.filesystem
                if candidate == root_id
            )
        except StopIteration:
            raise EnvironmentPermissionDenied(root_id, "access") from None


@dataclass(frozen=True)
class EnvironmentLocation:
    environment_id: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "environment_id": self.environment_id,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> "EnvironmentLocation":
        return cls(
            environment_id=value["environment_id"],
            path=value["path"],
        )


@dataclass(frozen=True)
class ResolvedEnvironmentPath:
    native_path: Path
    location: EnvironmentLocation
    workspace_membership: WorkspaceMembership

    def result_fields(self) -> dict[str, object]:
        return {
            "location": self.location.to_dict(),
            "workspace_membership": self.workspace_membership.to_dict(),
        }


@dataclass(frozen=True)
class EnvironmentSelection:
    environment_id: str
    workspace_view: WorkspaceViewSnapshot
    cwd: str

    def to_dict(self) -> dict[str, object]:
        return {
            "environment_id": self.environment_id,
            "workspace_view": self.workspace_view.to_dict(),
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "EnvironmentSelection":
        workspace_view = value["workspace_view"]
        if not isinstance(workspace_view, dict):
            raise ValueError("workspace_view 必须是 object")
        return cls(
            environment_id=str(value["environment_id"]),
            workspace_view=WorkspaceViewSnapshot.from_dict(workspace_view),
            cwd=str(value["cwd"]),
        )


class EnvironmentCommandExecutor(Protocol):
    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int,
    ) -> Any:
        ...


@dataclass(frozen=True)
class RuntimeAttachment:
    environment_instance_id: str
    command_executor: EnvironmentCommandExecutor
    process_sandbox: str = "unavailable"

    def __post_init__(self) -> None:
        if not self.environment_instance_id.strip():
            raise ValueError("environment instance id 不能为空")


@dataclass(frozen=True)
class EnvironmentBinding:
    environment_id: str
    workspace_view: WorkspaceViewSnapshot
    permission_binding: PermissionBinding
    cwd: Path
    shell_name: str
    shell_path: str
    runtime_attachment: RuntimeAttachment

    def __post_init__(self) -> None:
        resolved_cwd = self.cwd.resolve()
        self.workspace_view.membership(resolved_cwd)
        object.__setattr__(self, "cwd", resolved_cwd)

    @property
    def resolver(self) -> "EnvironmentPathResolver":
        return EnvironmentPathResolver(self)


class EnvironmentProvider(Protocol):
    async def attach(
        self,
        selection: EnvironmentSelection,
    ) -> EnvironmentBinding:
        ...


class LocalEnvironmentProvider:
    def __init__(
        self,
        command_executor: EnvironmentCommandExecutor,
        environment_id: str = "local",
        *,
        shell_name: str = "powershell",
        shell_path: str = "powershell.exe",
    ) -> None:
        if not environment_id or not environment_id.strip():
            raise ValueError("environment_id 不能为空")
        self.environment_id = environment_id
        self.shell_name = shell_name
        self.shell_path = shell_path
        self.command_executor = command_executor

    async def attach(
        self,
        selection: EnvironmentSelection,
    ) -> EnvironmentBinding:
        if selection.environment_id != self.environment_id:
            raise UnknownEnvironment(selection.environment_id)
        return EnvironmentBinding(
            environment_id=self.environment_id,
            workspace_view=selection.workspace_view,
            permission_binding=PermissionBinding.read_write(
                selection.workspace_view,
                network_access="unrestricted",
            ),
            cwd=Path(selection.cwd),
            shell_name=self.shell_name,
            shell_path=self.shell_path,
            runtime_attachment=RuntimeAttachment(
                environment_instance_id=self.environment_id,
                command_executor=self.command_executor,
            ),
        )


class EnvironmentPathResolver:
    def __init__(self, binding: EnvironmentBinding) -> None:
        self.binding = binding

    def resolve(
        self,
        raw_path: str,
        *,
        access: str = "read",
    ) -> ResolvedEnvironmentPath:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise InvalidEnvironmentPath("path 不能为空")
        if "\x00" in raw_path:
            raise InvalidEnvironmentPath("path 不能包含 NUL")
        candidate = Path(raw_path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute() or candidate.drive
            else (self.binding.cwd / candidate).resolve()
        )
        membership = self.binding.workspace_view.membership(resolved)
        permission = self.binding.permission_binding.access_for(
            membership.root_id
        )
        if access == "write" and permission is not FilesystemPermission.READ_WRITE:
            raise EnvironmentPermissionDenied(raw_path, access)
        return ResolvedEnvironmentPath(
            native_path=resolved,
            location=EnvironmentLocation(
                environment_id=(
                    self.binding.runtime_attachment.environment_instance_id
                ),
                path=resolved.as_uri(),
            ),
            workspace_membership=membership,
        )


def environment_error(exc: EnvironmentInputError) -> dict[str, str | bool]:
    return {"ok": False, "code": exc.code, "error": str(exc)}


def discover_host_roots() -> tuple[RootBinding, ...]:
    if os.name == "nt":
        roots = tuple(
            RootBinding(
                root_id=f"drive_{letter.lower()}",
                scope=WorkspaceScope.HOST,
                path=Path(f"{letter}:\\"),
            )
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{letter}:\\").is_dir()
        )
    else:
        roots = (
            RootBinding(
                root_id="filesystem",
                scope=WorkspaceScope.HOST,
                path=Path("/"),
            ),
        )
    if not roots:
        raise RuntimeError("未发现可访问的宿主机文件系统 root")
    return roots


def render_environment_context(binding: EnvironmentBinding) -> str:
    now = datetime.now().astimezone()
    roots = "\n".join(
        "    <root "
        f'id="{escape(root.root_id)}" scope="{root.scope.value}" '
        f'uri="{escape(root.path.as_uri())}" />'
        for root in binding.workspace_view.roots
    )
    permissions = "\n".join(
        "    <filesystem "
        f'root="{root_id}" access="{access.value}" />'
        for root_id, access in binding.permission_binding.filesystem
    )
    return "\n".join([
        "<environment_context>",
        (
            f'  <environment id="{escape(binding.environment_id)}" '
            f'kind="host" os="{os.name}" />'
        ),
        f"  <cwd>{escape(str(binding.cwd))}</cwd>",
        f"  <current_date>{now.date().isoformat()}</current_date>",
        f"  <timezone>{escape(str(now.tzinfo))}</timezone>",
        "  <workspace_roots>",
        roots,
        "  </workspace_roots>",
        "  <permissions>",
        permissions,
        "  </permissions>",
        (
            "  <network "
            f'access="{escape(binding.permission_binding.network_access)}" />'
        ),
        (
            "  <sandbox "
            f'process="{escape(binding.runtime_attachment.process_sandbox)}" />'
        ),
        "  <shell "
        f'name="{escape(binding.shell_name)}" '
        f'path="{escape(binding.shell_path)}" />',
        "</environment_context>",
    ])
