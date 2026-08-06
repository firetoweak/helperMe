from __future__ import annotations

from core.tool_registry import ToolSpec
from tools.command_execution import create_command_execution_spec
from tools.file_manage import create_file_manage_specs
from tools.file_read import create_file_read_specs
from tools.file_write import create_file_write_specs
from tools.get_changes import create_get_changes_specs
from tools.powershell_runner import PowerShellCommandRunner
from tools.workspace import WorkspaceSandboxes

# demo 仍是无状态内建工具，通过导入副作用注册。
import tools.demo  # noqa: F401


def create_workspace_tool_specs(
    workspaces: WorkspaceSandboxes,
    command_runner: PowerShellCommandRunner | None = None,
) -> list[ToolSpec]:
    runner = command_runner or PowerShellCommandRunner()
    return [
        *create_file_read_specs(workspaces),
        *create_file_write_specs(workspaces),
        *create_file_manage_specs(workspaces),
        *create_get_changes_specs(workspaces),
        create_command_execution_spec(workspaces, runner),
    ]
