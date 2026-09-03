from __future__ import annotations

from helperme.sandbox.api import EnvironmentBinding
from helperme.tools.builtin.command_execution import create_command_execution_spec
from helperme.tools.builtin.file_manage import create_file_manage_specs
from helperme.tools.builtin.file_read import create_file_read_specs
from helperme.tools.builtin.file_write import create_file_write_specs
from helperme.tools.builtin.get_changes import create_get_changes_specs
from helperme.tools.spec import ToolSpec


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
