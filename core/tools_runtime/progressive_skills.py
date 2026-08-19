from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import Any, Protocol

from pydantic import BaseModel, Field

from core.tool_registry import PydanticParameters, ToolSpec


LOAD_SKILL = "load_skill"
READ_SKILL_RESOURCE = "read_skill_resource"


@dataclass(frozen=True)
class SkillDescriptor:
    name: str
    description: str
    revision: int


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    description: str
    revision: int
    main_instructions: str
    skill_dir: str


class SkillLoadError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.data = data or {}


class SkillProvider(Protocol):
    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        ...

    async def load_skill(
        self,
        skill_id: str,
        expected_revision: int,
    ) -> LoadedSkill:
        ...

    async def read_resource(
        self,
        skill_id: str,
        expected_revision: int,
        relative_path: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class SessionSkillSnapshot:
    descriptors: tuple[SkillDescriptor, ...]

    @classmethod
    def capture(cls, provider: SkillProvider) -> "SessionSkillSnapshot":
        descriptors = tuple(sorted(
            provider.descriptors(), key=lambda item: item.name
        ))
        names = [item.name for item in descriptors]
        if len(names) != len(set(names)):
            raise ValueError("Session Skill snapshot 包含重复 name")
        return cls(descriptors)


@dataclass(frozen=True)
class SnapshotSkillProvider:
    provider: SkillProvider
    snapshot: SessionSkillSnapshot

    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        return self.snapshot.descriptors

    async def load_skill(
        self,
        skill_id: str,
        expected_revision: int,
    ) -> LoadedSkill:
        descriptor = self._descriptor(skill_id)
        if descriptor.revision != expected_revision:
            raise SkillLoadError(
                "SKILL_SNAPSHOT_MISMATCH",
                f"Skill {skill_id} 请求 revision 与 Session 快照不一致",
                hint="请从当前 Session Skill 目录重新选择。",
                data={"skill_id": skill_id},
            )
        self._validate_current_access(descriptor)
        return await self.provider.load_skill(skill_id, descriptor.revision)

    async def read_resource(
        self,
        skill_id: str,
        expected_revision: int,
        relative_path: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        descriptor = self._descriptor(skill_id)
        if descriptor.revision != expected_revision:
            raise SkillLoadError(
                "SKILL_SNAPSHOT_MISMATCH",
                f"Skill {skill_id} 请求 revision 与 Session 快照不一致",
                hint="请在新 Session 中重新加载。",
                data={"skill_id": skill_id},
            )
        self._validate_current_access(descriptor)
        return await self.provider.read_resource(
            skill_id,
            descriptor.revision,
            relative_path,
            offset,
            limit,
        )

    def _descriptor(self, skill_id: str) -> SkillDescriptor:
        descriptor = next(
            (
                item for item in self.snapshot.descriptors
                if item.name == skill_id
            ),
            None,
        )
        if descriptor is None:
            raise SkillLoadError(
                "SKILL_NOT_FOUND",
                f"Skill {skill_id} 不在当前 Session 目录中",
                hint="请从当前 Skill 目录选择有效 ID。",
                data={"skill_id": skill_id},
            )
        return descriptor

    def _validate_current_access(self, captured: SkillDescriptor) -> None:
        current = next(
            (
                item for item in self.provider.descriptors()
                if item.name == captured.name
            ),
            None,
        )
        if current is None or current.revision != captured.revision:
            raise SkillLoadError(
                "SKILL_SNAPSHOT_STALE",
                f"Skill {captured.name} 已在 Session 创建后变化",
                hint="创建新 Session 以使用最新 Skill 集合。",
                data={
                    "skill_id": captured.name,
                    "expected_revision": captured.revision,
                    "current_revision": (
                        current.revision if current is not None else None
                    ),
                },
            )


@dataclass(frozen=True)
class SkillBudget:
    max_catalog_chars: int = 20_000
    max_loaded_instruction_chars: int = 200_000

    def __post_init__(self) -> None:
        if self.max_catalog_chars <= 0 or self.max_loaded_instruction_chars <= 0:
            raise ValueError("Skill budget 必须大于 0")


@dataclass
class SkillLoadingState:
    loaded: dict[str, LoadedSkill] = field(default_factory=dict)


class LoadSkillInput(BaseModel):
    skill_id: str


class ReadSkillResourceInput(BaseModel):
    skill_id: str
    relative_path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20_000, ge=1, le=50_000)


def create_load_skill_spec(
    descriptors: tuple[SkillDescriptor, ...],
    state: SkillLoadingState,
    provider: SkillProvider,
    budget: SkillBudget,
) -> ToolSpec:
    by_id = {descriptor.name: descriptor for descriptor in descriptors}

    async def load_skill(input_data: LoadSkillInput) -> dict[str, Any]:
        descriptor = by_id.get(input_data.skill_id)
        if descriptor is None:
            return _error_result(SkillLoadError(
                "SKILL_NOT_FOUND",
                f"Skill {input_data.skill_id} 不在当前 Session 目录中",
                hint="请从 Skill 目录选择有效 ID。",
                data={"skill_id": input_data.skill_id},
            ))
        existing = state.loaded.get(input_data.skill_id)
        if existing is not None:
            return {
                "ok": True,
                "code": "SKILL_ALREADY_LOADED",
                "data": {
                    "skill_id": existing.name,
                    "skill_dir": existing.skill_dir,
                },
            }
        try:
            candidate = await provider.load_skill(
                descriptor.name,
                descriptor.revision,
            )
        except SkillLoadError as exc:
            return _error_result(exc)
        projected_chars = sum(
            len(item.main_instructions) for item in state.loaded.values()
        ) + len(candidate.main_instructions)
        if projected_chars > budget.max_loaded_instruction_chars:
            return _error_result(SkillLoadError(
                "SKILL_CONTEXT_LIMIT",
                "Skill 主指令累计超出当前 Turn 预算",
                hint="保留已加载 Skill，改用更少的 Skill 完成当前 Turn。",
                data={
                    "skill_id": candidate.name,
                    "projected_chars": projected_chars,
                    "limit_chars": budget.max_loaded_instruction_chars,
                },
            ))
        state.loaded[candidate.name] = candidate
        return {
            "ok": True,
            "code": "SKILL_LOADED",
            "data": {
                "skill_id": candidate.name,
                "skill_dir": candidate.skill_dir,
            },
        }

    return ToolSpec(
        name=LOAD_SKILL,
        description=(
            "为当前 Turn 加载一个 Skill。必须单独调用；"
            "完整主指令从下一 AgentStep 开始生效，不进入工具结果。"
        ),
        parameters=PydanticParameters(LoadSkillInput),
        handler=load_skill,
        exclusive_batch=True,
    )


def create_read_skill_resource_spec(
    state: SkillLoadingState,
    provider: SkillProvider,
) -> ToolSpec:
    async def read_resource(
        input_data: ReadSkillResourceInput,
    ) -> dict[str, Any]:
        loaded = state.loaded.get(input_data.skill_id)
        if loaded is None:
            return _error_result(SkillLoadError(
                "SKILL_NOT_LOADED",
                f"Skill {input_data.skill_id} 尚未在当前 Turn 加载",
                hint="先单独调用 load_skill，再读取其 supporting file。",
                data={"skill_id": input_data.skill_id},
            ))
        try:
            return await provider.read_resource(
                loaded.name,
                loaded.revision,
                input_data.relative_path,
                input_data.offset,
                input_data.limit,
            )
        except SkillLoadError as exc:
            return _error_result(exc)

    return ToolSpec(
        name=READ_SKILL_RESOURCE,
        description=(
            "按字符范围读取当前 Turn 已加载 Skill 的文本资源。"
            "relative_path 必须是 Skill Directory 内的相对路径。"
        ),
        parameters=PydanticParameters(ReadSkillResourceInput),
        handler=read_resource,
    )


def skill_runtime_instructions(
    descriptors: tuple[SkillDescriptor, ...],
    state: SkillLoadingState,
    budget: SkillBudget,
) -> list[str]:
    catalog_lines = [
        "可按需加载以下 Skill。需要其方法时，必须单独调用 load_skill；"
        "主指令从下一 AgentStep 开始生效。历史加载回执不代表当前 Turn 已加载："
    ]
    catalog_lines.extend(
        f"- {item.name}: {item.description}"
        + ("（已加载）" if item.name in state.loaded else "")
        for item in descriptors
    )
    catalog = "\n".join(catalog_lines)
    if len(catalog) > budget.max_catalog_chars:
        raise RuntimeError("SKILL_CATALOG_LIMIT: 完整 Skill 目录超出预算")
    prompts = [catalog]
    prompts.extend(
        "\n".join([
            f'<skill id="{item.name}" directory="{escape(item.skill_dir, quote=True)}">',
            item.main_instructions,
            "</skill>",
        ])
        for item in state.loaded.values()
    )
    return prompts


def _error_result(exc: SkillLoadError) -> dict[str, Any]:
    return {
        "ok": False,
        "code": exc.code,
        "data": exc.data or None,
        "error": exc.message,
        "hint": exc.hint,
    }
