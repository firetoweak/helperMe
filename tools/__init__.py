from __future__ import annotations

from core.environment import EnvironmentBinding
from core.tool_registry import ToolSpec
from tools.command_execution import create_command_execution_spec
from tools.file_manage import create_file_manage_specs
from tools.file_read import create_file_read_specs
from tools.file_write import create_file_write_specs
from tools.get_changes import create_get_changes_specs

# demo 仍是无状态内建工具，通过导入副作用注册。
import tools.demo  # noqa: F401


def create_environment_tool_specs(
    binding: EnvironmentBinding,
) -> list[ToolSpec]:
    return [
        *create_file_read_specs(binding),
        *create_file_write_specs(binding),
        *create_file_manage_specs(binding),
        *create_get_changes_specs(binding),
        create_command_execution_spec(binding),
    ]
