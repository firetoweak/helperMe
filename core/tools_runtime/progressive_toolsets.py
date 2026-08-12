from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from core.tool_registry import PydanticParameters, ToolSpec


LOAD_TOOLSET = "load_toolset"


@dataclass(frozen=True)
class ToolsetDescriptor:
    id: str
    description: str


class ToolsetProvider(Protocol):
    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        ...

    def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        ...


@dataclass
class ToolsetLoadingState:
    loaded_ids: set[str] = field(default_factory=set)


class LoadToolsetInput(BaseModel):
    toolset_id: str


def create_load_toolset_spec(
    descriptors: tuple[ToolsetDescriptor, ...],
    state: ToolsetLoadingState,
) -> ToolSpec:
    available_ids = {descriptor.id for descriptor in descriptors}

    async def load_toolset(input_data: LoadToolsetInput) -> dict:
        if input_data.toolset_id not in available_ids:
            return {
                "ok": False,
                "code": "TOOLSET_NOT_FOUND",
                "data": {"toolset_id": input_data.toolset_id},
                "error": f"Toolset {input_data.toolset_id} not found",
                "hint": "请从可选 Toolset 目录中选择有效 ID。",
            }
        state.loaded_ids.add(input_data.toolset_id)
        return {
            "ok": True,
            "code": "TOOLSET_LOADED",
            "data": {"toolset_id": input_data.toolset_id},
        }

    return ToolSpec(
        name=LOAD_TOOLSET,
        description="为当前 Run 加载一个 Toolset；其中的工具从下一轮开始可用。",
        parameters=PydanticParameters(LoadToolsetInput),
        handler=load_toolset,
    )


def toolset_catalog_instruction(
    descriptors: tuple[ToolsetDescriptor, ...],
    state: ToolsetLoadingState,
) -> str:
    lines = [
        "可按需加载以下 Toolset。需要其中能力时，调用 load_toolset；加载后的工具从下一轮开始可用："
    ]
    lines.extend(
        f"- {descriptor.id}: {descriptor.description}"
        + ("（已加载）" if descriptor.id in state.loaded_ids else "")
        for descriptor in descriptors
    )
    return "\n".join(lines)
