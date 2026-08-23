from __future__ import annotations

from collections.abc import Mapping

from helperme.assistant.toolsets import (
    LoadedTool,
    ToolsetDescriptor,
    ToolsetLoadError,
)
from helperme.assistant.tool_results import runtime_tool_result
from helperme.mcp.toolsets import ToolsetLoadError as ProviderLoadError


class McpToolsetAdapter:
    """Translate the MCP catalog to the Assistant Toolset port."""

    def __init__(self, mcp) -> None:
        self._provider = mcp.toolset_provider

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        return tuple(
            ToolsetDescriptor(
                item.id,
                item.description,
                item.revision,
            )
            for item in self._provider.descriptors()
        )

    async def load(self, toolset_id: str) -> tuple[LoadedTool, ...]:
        try:
            specs = await self._provider.tool_specs(toolset_id)
        except ProviderLoadError as exc:
            raise ToolsetLoadError(
                exc.code,
                exc.message,
                hint=exc.hint,
                data=exc.data,
            ) from exc
        return tuple(_loaded_from_spec(spec) for spec in specs)


def _loaded_from_spec(spec) -> LoadedTool:
    async def execute(arguments: Mapping[str, object]) -> object:
        return runtime_tool_result(await spec.handler(dict(arguments)))

    return LoadedTool(
        name=spec.name,
        description=spec.description,
        parameters=dict(spec.parameters.schema()),
        execute=execute,
        requires_authorization=spec.requires_authorization,
    )
