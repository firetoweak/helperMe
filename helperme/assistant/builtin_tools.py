from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from helperme.assistant.tool_results import runtime_tool_result
from helperme.runtime.model import AuthorizationPolicy
from helperme.sandbox.api import EnvironmentSelection
from helperme.sandbox.local.provider import create_local_environment_provider
from helperme.sandbox.registry import WorkspaceRecord, workspace_view
from helperme.tools.executor import ToolsExecutor
from helperme.tools.registry import BUILTIN_TOOL_REGISTRY
from helperme.tools.builtin import create_environment_tool_specs


@dataclass(frozen=True, slots=True)
class BuiltinToolRunner:
    schemas: tuple[dict[str, object], ...]
    _executor: ToolsExecutor

    async def execute(self, name: str, arguments: Mapping[str, object]) -> object:
        return runtime_tool_result(
            await self._executor.execute_parsed(name, arguments)
        )

    def names(self) -> tuple[str, ...]:
        names: list[str] = []
        for schema in self.schemas:
            if set(schema) != {"type", "function"} or schema["type"] != "function":
                raise ValueError("builtin tool schema envelope 无效")
            function = schema["function"]
            if not isinstance(function, dict):
                raise ValueError("builtin tool schema function 必须是 object")
            name = function.get("name")
            if type(name) is not str or not name:
                raise ValueError("builtin tool schema name 必须是非空 string")
            names.append(name)
        if len(names) != len(set(names)):
            raise ValueError("builtin tool schemas 包含重复 name")
        return tuple(names)

    def requires_authorization(self, name: str) -> bool | AuthorizationPolicy:
        spec = self._executor.registry.get(name)
        if spec is None:
            raise KeyError(name)
        return spec.requires_authorization


async def build_builtin_tools(workspace: WorkspaceRecord) -> BuiltinToolRunner:
    view = workspace_view(workspace)
    provider = create_local_environment_provider()
    binding = await provider.attach(EnvironmentSelection(
        environment_id=provider.environment_id,
        workspace_view=view,
        cwd=str(workspace.task_root),
    ))
    registry = BUILTIN_TOOL_REGISTRY.clone()
    for spec in create_environment_tool_specs(binding):
        registry.register(spec)
    return BuiltinToolRunner(
        schemas=tuple(registry.get_tools()),
        _executor=ToolsExecutor(registry),
    )
