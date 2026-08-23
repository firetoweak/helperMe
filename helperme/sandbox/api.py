from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
import os
from pathlib import Path
from typing import Any, Protocol

from helperme.sandbox.workspace import (
    EnvironmentInputError,
    PermissionBinding,
    WorkspacePathResolver,
    WorkspaceViewSnapshot,
)


class UnknownEnvironment(EnvironmentInputError):
    code = "UNKNOWN_ENVIRONMENT"

    def __init__(self, environment_id: str) -> None:
        super().__init__(f"未知的 Environment: {environment_id}")


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
        if set(value) != {"environment_id", "workspace_view", "cwd"}:
            raise ValueError("Environment selection 字段不匹配")
        workspace_view = value["workspace_view"]
        if not isinstance(workspace_view, dict):
            raise ValueError("workspace_view 必须是 object")
        environment_id = value["environment_id"]
        cwd = value["cwd"]
        if type(environment_id) is not str or type(cwd) is not str:
            raise ValueError("environment_id/cwd 必须是 string")
        return cls(
            environment_id=environment_id,
            workspace_view=WorkspaceViewSnapshot.from_dict(workspace_view),
            cwd=cwd,
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
class ExecutionAttachment:
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
    execution_attachment: ExecutionAttachment

    def __post_init__(self) -> None:
        resolved_cwd = self.cwd.resolve()
        self.workspace_view.membership(resolved_cwd)
        object.__setattr__(self, "cwd", resolved_cwd)

    @property
    def resolver(self) -> WorkspacePathResolver:
        return WorkspacePathResolver(self)


class EnvironmentProvider(Protocol):
    async def attach(
        self,
        selection: EnvironmentSelection,
    ) -> EnvironmentBinding:
        ...


def environment_error(exc: EnvironmentInputError) -> dict[str, str | bool]:
    return {"ok": False, "code": exc.code, "error": str(exc)}


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
            f'process="{escape(binding.execution_attachment.process_sandbox)}" />'
        ),
        "  <shell "
        f'name="{escape(binding.shell_name)}" '
        f'path="{escape(binding.shell_path)}" />',
        "</environment_context>",
    ])
