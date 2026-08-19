from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from core.tools_runtime.progressive_skills import (
    LoadedSkill,
    SkillDescriptor,
    SkillLoadError,
)
from plugins.skills.models import SkillRecord
from plugins.skills.package import (
    LocalSkillPackageReader,
    SkillPackageError,
    validate_relative_skill_path,
)
from plugins.skills.registry import SkillRegistry


class InstalledSkillProvider:
    """Runtime 只消费 Registry 中 enabled 且内容一致的 Skill。"""

    def __init__(
        self,
        registry: SkillRegistry,
        package_reader: LocalSkillPackageReader | None = None,
    ) -> None:
        self.registry = registry
        self.package_reader = package_reader or LocalSkillPackageReader()
        self.packages_root = registry.root / "packages"

    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        return tuple(
            SkillDescriptor(
                name=record.name,
                description=record.description,
                revision=record.revision,
            )
            for record in self._read_records_sync()
            if record.enabled
        )

    async def load_skill(
        self,
        skill_id: str,
        expected_revision: int,
    ) -> LoadedSkill:
        record = await self._require_runtime_record(
            skill_id,
            expected_revision,
        )
        package_directory, bundle = self._validated_bundle(record)
        return LoadedSkill(
            name=record.name,
            description=record.description,
            revision=record.revision,
            main_instructions=bundle.main_instructions,
            skill_dir=str(package_directory),
        )

    async def read_resource(
        self,
        skill_id: str,
        expected_revision: int,
        relative_path: str,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        record = await self._require_runtime_record(
            skill_id,
            expected_revision,
        )
        package_directory, _ = self._validated_bundle(record)
        try:
            normalized = validate_relative_skill_path(relative_path)
        except SkillPackageError as exc:
            raise SkillLoadError(
                "INVALID_SKILL_RESOURCE_PATH",
                str(exc),
                hint="使用当前 Skill Directory 内的规范相对路径。",
                data={"skill_id": skill_id, "relative_path": relative_path},
            ) from exc
        resource = package_directory.joinpath(*PurePosixPath(normalized).parts)
        resolved = resource.resolve()
        if not resolved.is_relative_to(package_directory):
            raise SkillLoadError(
                "INVALID_SKILL_RESOURCE_PATH",
                f"Skill 资源路径越界: {relative_path}",
                hint="只能读取当前已加载 Skill 的包内文件。",
            )
        if not resolved.is_file():
            raise SkillLoadError(
                "SKILL_RESOURCE_NOT_FOUND",
                f"Skill 资源不存在: {relative_path}",
                hint="根据 SKILL.md 中列出的 supporting file 路径重试。",
                data={"skill_id": skill_id, "relative_path": relative_path},
            )
        try:
            content = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLoadError(
                "SKILL_RESOURCE_NOT_TEXT",
                f"Skill 资源不是 UTF-8 文本: {relative_path}",
                hint="二进制 assets 不通过 read_skill_resource 读取。",
            ) from exc
        if offset > len(content):
            raise SkillLoadError(
                "SKILL_RESOURCE_OFFSET_OUT_OF_RANGE",
                f"offset={offset} 超出资源长度 {len(content)}",
                hint="使用上一次结果的 next_offset，或从 0 开始。",
            )
        end = min(offset + limit, len(content))
        return {
            "ok": True,
            "code": "SKILL_RESOURCE_READ",
            "data": {
                "skill_id": skill_id,
                "relative_path": normalized,
                "content": content[offset:end],
                "offset": offset,
                "next_offset": end if end < len(content) else None,
                "total_chars": len(content),
            },
        }

    async def _require_runtime_record(
        self,
        skill_id: str,
        expected_revision: int,
    ) -> SkillRecord:
        record = await self.registry.get(skill_id)
        if record is None or not record.enabled:
            raise SkillLoadError(
                "SKILL_SNAPSHOT_STALE",
                f"Skill {skill_id} 已被停用或删除",
                hint="创建新 Session 以捕获最新 Skill 目录。",
                data={"skill_id": skill_id},
            )
        if record.revision != expected_revision:
            raise SkillLoadError(
                "SKILL_SNAPSHOT_STALE",
                f"Skill {skill_id} 已在 Session 创建后变化",
                hint="创建新 Session 以使用新 revision。",
                data={
                    "skill_id": skill_id,
                    "expected_revision": expected_revision,
                    "current_revision": record.revision,
                },
            )
        return record

    def _validated_bundle(self, record: SkillRecord):
        directory = (self.packages_root / record.name).resolve()
        if (
            not directory.is_relative_to(self.packages_root.resolve())
            or not directory.is_relative_to(self.registry.root)
        ):
            raise RuntimeError("Skill Registry 推导出越界包路径")
        if directory.name != record.name or not directory.is_dir():
            raise RuntimeError(f"已登记 Skill 包目录丢失: {record.name}")
        bundle = self.package_reader.read(directory)
        if bundle.name != record.name:
            raise RuntimeError(f"Skill 严格身份不一致: {record.name}")
        if bundle.description != record.description:
            raise RuntimeError(
                f"Skill Registry description 与包不一致: {record.name}"
            )
        if bundle.content_hash != record.content_hash:
            raise RuntimeError(f"Skill Registry hash 与包不一致: {record.name}")
        return directory, bundle

    def _read_records_sync(self) -> tuple[SkillRecord, ...]:
        if not self.registry.path.exists():
            return ()
        payload = json.loads(self.registry.path.read_text(encoding="utf-8"))
        skills = payload.get("skills", payload)
        if not isinstance(skills, list):
            raise ValueError("Skill registry.json 格式无效")
        records = tuple(SkillRecord.from_dict(item) for item in skills)
        names = [item.name for item in records]
        if len(names) != len(set(names)):
            raise ValueError("Skill registry.json 包含重复 name")
        return tuple(sorted(records, key=lambda item: item.name))
