from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from core.tool_registry import PydanticParameters, ToolSpec


LOAD_TOOLSET = "load_toolset"


class ToolsetLoadError(Exception):
    """Toolset 加载失败；由 load_toolset 转换为模型可修正的工具错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        data: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.data = data or {}


@dataclass(frozen=True)
class ToolsetDescriptor:
    id: str
    description: str


class ToolsetProvider(Protocol):
    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        ...

    async def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        ...


@dataclass(frozen=True)
class CompositeToolsetProvider:
    """合并多个 Provider；ID 冲突在构造期失败。"""

    providers: tuple[ToolsetProvider, ...]

    def __post_init__(self) -> None:
        seen: dict[str, int] = {}
        for index, provider in enumerate(self.providers):
            for descriptor in provider.descriptors():
                if descriptor.id in seen:
                    raise ValueError(
                        "duplicate toolset id across providers: "
                        f"{descriptor.id!r}"
                    )
                seen[descriptor.id] = index

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        return tuple(
            descriptor
            for provider in self.providers
            for descriptor in provider.descriptors()
        )

    async def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        for provider in self.providers:
            ids = {descriptor.id for descriptor in provider.descriptors()}
            if toolset_id in ids:
                return await provider.tool_specs(toolset_id)
        raise ToolsetLoadError(
            "TOOLSET_NOT_FOUND",
            f"Toolset {toolset_id} not found",
            hint="请从可选 Toolset 目录中选择有效 ID。",
            data={"toolset_id": toolset_id},
        )


@dataclass
class ToolsetLoadingState:
    loaded_specs: dict[str, tuple[ToolSpec, ...]] = field(default_factory=dict)

    @property
    def loaded_ids(self) -> set[str]:
        return set(self.loaded_specs)


class LoadToolsetInput(BaseModel):
    toolset_id: str


def create_load_toolset_spec(
    descriptors: tuple[ToolsetDescriptor, ...],
    state: ToolsetLoadingState,
    provider: ToolsetProvider,
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
        if input_data.toolset_id not in state.loaded_specs:
            try:
                specs = await provider.tool_specs(input_data.toolset_id)
            except ToolsetLoadError as exc:
                return {
                    "ok": False,
                    "code": exc.code,
                    "data": {
                        "toolset_id": input_data.toolset_id,
                        **exc.data,
                    },
                    "error": exc.message,
                    "hint": exc.hint,
                }
            state.loaded_specs[input_data.toolset_id] = tuple(specs)
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
        + ("（已加载）" if descriptor.id in state.loaded_specs else "")
        for descriptor in descriptors
    )
    return "\n".join(lines)
