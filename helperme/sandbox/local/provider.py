from __future__ import annotations

import os
from pathlib import Path

from helperme.sandbox.api import (
    EnvironmentBinding,
    EnvironmentSelection,
    ExecutionAttachment,
    UnknownEnvironment,
)
from helperme.sandbox.command import EnvironmentCommandExecutor
from helperme.sandbox.workspace import (
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
)


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
            execution_attachment=ExecutionAttachment(
                environment_instance_id=self.environment_id,
                command_executor=self.command_executor,
            ),
        )


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
