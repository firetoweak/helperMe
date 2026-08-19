from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
from typing import Protocol

from plugins.skills.models import SkillBundle, SkillRecord
from plugins.skills.package import (
    LocalSkillPackageReader,
    SkillPackageError,
    write_skill_bundle,
)


class SkillRegistryWriter(Protocol):
    async def get(self, skill_id: str) -> SkillRecord | None:
        ...

    async def add(self, record: SkillRecord) -> SkillRecord:
        ...


class LocalSkillInstaller:
    """本地目录安装：冻结包、staging 校验、原子发布、提交 Registry。"""

    def __init__(
        self,
        skills_root: Path,
        registry: SkillRegistryWriter,
        package_reader: LocalSkillPackageReader | None = None,
    ) -> None:
        self.skills_root = skills_root.resolve()
        self.packages_root = self.skills_root / "packages"
        self.staging_root = self.skills_root / ".staging"
        self.registry = registry
        self.package_reader = package_reader or LocalSkillPackageReader()
        self._lock = asyncio.Lock()

    async def install(self, source_directory: Path) -> SkillRecord:
        bundle = self.package_reader.read(source_directory)
        return await self.install_bundle(bundle)

    async def install_bundle(self, bundle: SkillBundle) -> SkillRecord:
        async with self._lock:
            existing = await self.registry.get(bundle.name)
            if existing is not None:
                raise ValueError(f"Skill 已安装: {bundle.name}")
            target = self._target(bundle.name)
            if target.exists():
                raise RuntimeError(
                    f"Skill 安装目录已存在但未登记: {target}"
                )

            self.validate_managed_path(self.packages_root)
            self.validate_managed_path(self.staging_root)
            self.packages_root.mkdir(parents=True, exist_ok=True)
            self.staging_root.mkdir(parents=True, exist_ok=True)
            self.validate_managed_path(self.packages_root)
            self.validate_managed_path(self.staging_root)
            staging_parent = Path(tempfile.mkdtemp(
                prefix=f"{bundle.name}-",
                dir=self.staging_root,
            ))
            staging_package = staging_parent / bundle.name
            published = False
            try:
                write_skill_bundle(staging_package, bundle)
                staged = self.package_reader.read(staging_package)
                self._assert_same_bundle(bundle, staged)
                os.replace(staging_package, target)
                published = True
                record = SkillRecord(
                    name=bundle.name,
                    description=bundle.description,
                    source=bundle.source,
                    resolved_ref=bundle.resolved_ref,
                    content_hash=bundle.content_hash,
                    enabled=False,
                )
                try:
                    return await self.registry.add(record)
                except BaseException:
                    shutil.rmtree(target)
                    published = False
                    raise
            finally:
                if staging_parent.exists():
                    shutil.rmtree(staging_parent)
                if published and not target.is_dir():
                    raise RuntimeError("Skill 发布后目录意外丢失")

    def _target(self, skill_id: str) -> Path:
        target = (self.packages_root / skill_id).resolve()
        if (
            not target.is_relative_to(self.packages_root.resolve())
            or not target.is_relative_to(self.skills_root)
        ):
            raise SkillPackageError("Skill 安装目标越界")
        return target

    def validate_managed_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.skills_root):
            raise SkillPackageError(f"Skill 管理目录越界: {path}")
        return resolved

    @staticmethod
    def _assert_same_bundle(expected: SkillBundle, actual: SkillBundle) -> None:
        if (
            actual.name != expected.name
            or actual.description != expected.description
            or actual.content_hash != expected.content_hash
        ):
            raise RuntimeError("Skill staging 校验结果与冻结候选不一致")
