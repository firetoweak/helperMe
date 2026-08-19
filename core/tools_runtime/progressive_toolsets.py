from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Protocol, runtime_checkable
from types import MappingProxyType

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
    revision: int = 1


@dataclass(frozen=True)
class SessionCapabilitySnapshot:
    toolsets: Mapping[str, int]
    provider_token: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "toolsets",
            MappingProxyType(dict(self.toolsets)),
        )

    @classmethod
    def capture(
        cls,
        provider: "ToolsetProvider",
    ) -> "SessionCapabilitySnapshot":
        token = (
            provider.toolset_snapshot_token()
            if isinstance(provider, ToolsetSnapshotTokenProvider)
            else None
        )
        return cls(
            {
                descriptor.id: descriptor.revision
                for descriptor in provider.descriptors()
            },
            provider_token=token,
        )


class ToolsetProvider(Protocol):
    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        ...

    async def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        ...


@runtime_checkable
class ToolsetAccessValidator(Protocol):
    async def validate_toolset_access(self, toolset_id: str) -> None:
        ...


@runtime_checkable
class ToolsetSnapshotTokenProvider(Protocol):
    def toolset_snapshot_token(self) -> object:
        ...


@dataclass(frozen=True)
class SnapshotToolsetProvider:
    provider: ToolsetProvider
    snapshot: SessionCapabilitySnapshot

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        return tuple(
            descriptor
            for descriptor in self.provider.descriptors()
            if self.snapshot.toolsets.get(descriptor.id)
            == descriptor.revision
        )

    async def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        await self.validate_toolset_access(toolset_id)
        specs = await self.provider.tool_specs(toolset_id)
        return tuple(self._guard_spec(spec, toolset_id) for spec in specs)

    async def validate_toolset_access(self, toolset_id: str) -> None:
        current = {
            descriptor.id: descriptor.revision
            for descriptor in self.provider.descriptors()
        }
        current_token = (
            self.provider.toolset_snapshot_token()
            if isinstance(self.provider, ToolsetSnapshotTokenProvider)
            else None
        )
        if (
            current != dict(self.snapshot.toolsets)
            or current_token != self.snapshot.provider_token
        ):
            raise ToolsetLoadError(
                "SESSION_CAPABILITIES_STALE",
                "当前 Session 的能力快照已过期",
                hint="请创建新 Session 后使用最新能力配置。",
                data={
                    "toolset_id": toolset_id,
                    "session_toolsets": dict(self.snapshot.toolsets),
                    "current_toolsets": current,
                },
            )
        if toolset_id not in self.snapshot.toolsets:
            raise ToolsetLoadError(
                "TOOLSET_NOT_FOUND",
                f"Toolset {toolset_id} not found",
                hint="请从可选 Toolset 目录中选择有效 ID。",
                data={"toolset_id": toolset_id},
            )

    def _guard_spec(self, spec: ToolSpec, toolset_id: str) -> ToolSpec:
        async def guarded_handler(input_data):
            try:
                await self.validate_toolset_access(toolset_id)
            except ToolsetLoadError as exc:
                return {
                    "ok": False,
                    "code": exc.code,
                    "data": exc.data,
                    "error": exc.message,
                    "hint": exc.hint,
                }
            return await spec.handler(input_data)

        return replace(spec, handler=guarded_handler)


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

    def toolset_snapshot_token(self) -> object:
        return tuple(
            provider.toolset_snapshot_token()
            if isinstance(provider, ToolsetSnapshotTokenProvider)
            else tuple(
                (item.id, item.revision)
                for item in provider.descriptors()
            )
            for provider in self.providers
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
        if isinstance(provider, ToolsetAccessValidator):
            try:
                await provider.validate_toolset_access(input_data.toolset_id)
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
        tools = [
            {"name": spec.name, "description": spec.description}
            for spec in state.loaded_specs[input_data.toolset_id]
        ]
        return {
            "ok": True,
            "code": "TOOLSET_LOADED",
            "data": {
                "toolset_id": input_data.toolset_id,
                "tools": tools,
            },
        }

    return ToolSpec(
        name=LOAD_TOOLSET,
        description=(
            "为当前 Turn 加载一个 Toolset，并返回本次发现的工具名称与描述；"
            "其中的工具从下一个 AgentStep 开始可用。加载状态不会延续到后续 Turn。"
        ),
        parameters=PydanticParameters(LoadToolsetInput),
        handler=load_toolset,
    )


def toolset_catalog_instruction(
    descriptors: tuple[ToolsetDescriptor, ...],
    state: ToolsetLoadingState,
) -> str:
    lines = [
        "可按需加载以下 Toolset。需要其中能力时，调用 load_toolset；加载后的工具从下一个 AgentStep 开始可用。"
        "“已加载”仅表示当前 Turn；历史工具结果只表示过去的发现事实，不代表当前可调用。"
        "只能调用当前轮 tools 中实际暴露的精确名称："
    ]
    lines.extend(
        f"- {descriptor.id}: {descriptor.description}"
        + ("（已加载）" if descriptor.id in state.loaded_specs else "")
        for descriptor in descriptors
    )
    return "\n".join(lines)
