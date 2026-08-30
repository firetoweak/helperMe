from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from helperme.assistant.tool_results import runtime_tool_result
from helperme.config import AssistantConfig
from helperme.sandbox.api import EnvironmentSelection
from helperme.sandbox.local.provider import (
    create_local_environment_provider,
    discover_host_roots,
)
from helperme.sandbox.workspace import (
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from helperme.tools.executor import ToolsExecutor
from helperme.tools.registry import BUILTIN_TOOL_REGISTRY
from helperme.tools.builtin import create_environment_tool_specs


@dataclass(frozen=True, slots=True)
class BuiltinToolRunner:
    schemas: tuple[dict[str, object], ...]
    _executor: ToolsExecutor

    async def execute(self, name: str, arguments: Mapping[str, object]) -> object:
        return runtime_tool_result(
            await self._executor.execute(
                name,
                json.dumps(dict(arguments), ensure_ascii=False),
            )
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

    def requires_authorization(self, name: str) -> bool:
        spec = self._executor.registry.get(name)
        if spec is None:
            raise KeyError(name)
        return spec.requires_authorization


async def build_builtin_tools(config: AssistantConfig) -> BuiltinToolRunner:
    workspace_roots = {"project": config.workspace_root}
    effective = dict(workspace_roots)
    if config.full_access:
        host_roots = {
            root.root_id: root.path for root in discover_host_roots()
        }
        duplicated = effective.keys() & host_roots.keys()
        if duplicated:
            raise ValueError(
                "显式 workspace root 与 Host root 名称冲突: "
                f"{sorted(duplicated)}"
            )
        effective.update(host_roots)
    task_root_ids = set(workspace_roots)
    view = WorkspaceViewSnapshot(tuple(
        RootBinding(
            root_id=name,
            scope=(
                WorkspaceScope.TASK
                if name in task_root_ids
                else WorkspaceScope.HOST
            ),
            path=root,
        )
        for name, root in effective.items()
    ))
    provider = create_local_environment_provider()
    binding = await provider.attach(EnvironmentSelection(
        environment_id=provider.environment_id,
        workspace_view=view,
        cwd=str(next(iter(workspace_roots.values())).resolve()),
    ))
    registry = BUILTIN_TOOL_REGISTRY.clone()
    for spec in create_environment_tool_specs(binding):
        registry.register(spec)
    return BuiltinToolRunner(
        schemas=tuple(registry.get_tools()),
        _executor=ToolsExecutor(registry),
    )
