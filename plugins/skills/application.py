from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import shutil
import tempfile
from typing import Callable

from core.agent_workspace import AgentWorkspace
from plugins.skills.installer import LocalSkillInstaller
from plugins.skills.models import SkillBundle, SkillRecord, validate_skill_id
from plugins.skills.package import LocalSkillPackageReader
from plugins.skills.package import write_skill_bundle
from plugins.skills.registry import SkillRegistry
from plugins.skills.provider import InstalledSkillProvider
from plugins.skills.updates import SkillCandidateStore
from plugins.skills.models import SkillUpdateCandidate, SkillUpdateReport
from plugins.skills.models import SkillSourceRef
from plugins.skills.sources import SkillSourceRouter
from plugins.skills.summarizer import SkillDiffSummarizer
from plugins.skills.install_candidates import SkillInstallCandidateStore
from plugins.skills.models import SkillInstallCandidate


@dataclass(frozen=True)
class SkillInspection:
    record: SkillRecord
    files: tuple[tuple[str, int], ...]
    main_instruction_chars: int


class SkillApplicationService:
    """Skill 用户控制面：安装、检查、启停与卸载。"""

    def __init__(
        self,
        workspace: AgentWorkspace,
        registry: SkillRegistry | None = None,
        package_reader: LocalSkillPackageReader | None = None,
        *,
        max_catalog_chars: int = 20_000,
        has_active_turns: Callable[[], bool] | None = None,
        source_router: SkillSourceRouter | None = None,
        diff_summarizer: SkillDiffSummarizer | None = None,
    ) -> None:
        if max_catalog_chars <= 0:
            raise ValueError("max_catalog_chars 必须大于 0")
        resolved_skills_root = workspace.skills_root.resolve()
        if not resolved_skills_root.is_relative_to(workspace.root):
            raise ValueError("Agent Workspace skills root 不能通过链接越界")
        self.workspace = workspace
        self.registry = registry or SkillRegistry.from_agent_workspace(workspace)
        self.package_reader = package_reader or LocalSkillPackageReader()
        self.installer = LocalSkillInstaller(
            workspace.skills_root,
            self.registry,
            self.package_reader,
        )
        self.skill_provider = InstalledSkillProvider(
            self.registry,
            self.package_reader,
        )
        self.max_catalog_chars = max_catalog_chars
        self.candidate_store = SkillCandidateStore(
            workspace.skills_root,
            self.package_reader,
        )
        self._has_active_turns = has_active_turns or (lambda: False)
        self.source_router = source_router or SkillSourceRouter(
            self.package_reader
        )
        self.diff_summarizer = diff_summarizer
        self.install_candidates = SkillInstallCandidateStore(
            workspace.skills_root,
            self.package_reader,
        )
        self._management_lock = asyncio.Lock()

    def bind_active_turn_guard(self, guard: Callable[[], bool]) -> None:
        self._has_active_turns = guard

    async def list_skills(self) -> tuple[SkillRecord, ...]:
        return await self.registry.list_skills()

    async def install_local(self, source_directory: Path) -> SkillRecord:
        return await self.install_source(SkillSourceRef(
            "local",
            str(source_directory.resolve()),
        ))

    async def install_source(self, source: SkillSourceRef) -> SkillRecord:
        bundle = await self.source_router.fetch(source)
        async with self._management_lock:
            return await self.installer.install_bundle(bundle)

    async def prepare_install(
        self,
        source: SkillSourceRef,
    ) -> SkillInstallCandidate:
        bundle = await self.source_router.fetch(source)
        async with self._management_lock:
            if await self.registry.get(bundle.name) is not None:
                raise ValueError(f"Skill 已安装: {bundle.name}")
            return self.install_candidates.freeze(bundle)

    async def install_frozen(
        self,
        skill_id: str,
        content_hash: str,
    ) -> SkillRecord:
        candidate, bundle = self.install_candidates.load(
            skill_id,
            content_hash,
        )
        if candidate.skill_id != bundle.name:
            raise RuntimeError("install candidate 严格身份不一致")
        async with self._management_lock:
            return await self.installer.install_bundle(bundle)

    async def inspect(self, skill_id: str) -> SkillInspection:
        record, bundle = await self._validated_bundle(skill_id)
        return SkillInspection(
            record=record,
            files=tuple(
                (item.relative_path, item.size) for item in bundle.files
            ),
            main_instruction_chars=len(bundle.main_instructions),
        )

    async def test_skill(self, skill_id: str) -> SkillInspection:
        return await self.inspect(skill_id)

    async def set_enabled(
        self,
        skill_id: str,
        enabled: bool,
    ) -> SkillRecord:
        async with self._management_lock:
            return await self._set_enabled_locked(skill_id, enabled)

    async def enable_frozen(
        self,
        skill_id: str,
        expected_revision: int,
        expected_hash: str,
    ) -> SkillRecord:
        async with self._management_lock:
            record = await self.registry.get(skill_id)
            if record is None:
                raise KeyError(skill_id)
            if (
                record.revision != expected_revision
                or record.content_hash != expected_hash
            ):
                raise ValueError(
                    f"Skill `{skill_id}` 已在审批前变化，冻结方案过期"
                )
            return await self._set_enabled_locked(skill_id, True)

    async def _set_enabled_locked(
        self,
        skill_id: str,
        enabled: bool,
    ) -> SkillRecord:
        if enabled:
            await self._validated_bundle(skill_id)
            records = await self.registry.list_skills()
            enabled_records = tuple(
                item
                for item in records
                if item.enabled or item.name == skill_id
            )
            catalog = self._catalog_text(enabled_records)
            if len(catalog) > self.max_catalog_chars:
                raise ValueError(
                    "SKILL_CATALOG_LIMIT: 启用后完整 Skill 目录超出预算"
                )
        return await self.registry.set_enabled(skill_id, enabled)

    async def check_update(
        self,
        skill_id: str,
        replacement_source: SkillSourceRef | None = None,
    ) -> SkillUpdateReport:
        async with self._management_lock:
            record, installed = await self._validated_bundle(skill_id)
            candidate_bundle = await self.source_router.fetch(
                replacement_source or record.source
            )
            candidate = self.candidate_store.freeze(
                record,
                installed,
                candidate_bundle,
            )
            if self.diff_summarizer is None:
                return SkillUpdateReport(
                    candidate,
                    summary_error="未配置 Skill 更新概括模型",
                )
            try:
                summary = await self.diff_summarizer.summarize(
                    candidate,
                    installed.main_instructions,
                    candidate_bundle.main_instructions,
                )
            except Exception as exc:
                return SkillUpdateReport(
                    candidate,
                    summary_error=(
                        "Skill 更新概括生成失败: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            return SkillUpdateReport(candidate, semantic_summary=summary)

    async def update(
        self,
        skill_id: str,
        candidate_hash: str,
    ) -> SkillRecord:
        async with self._management_lock:
            if self._has_active_turns():
                raise ValueError("活动 Turn 期间不允许替换 Skill 包")
            current, _ = await self._validated_bundle(skill_id)
            candidate, bundle = self.candidate_store.load(
                skill_id,
                candidate_hash,
            )
            if (
                candidate.old_revision != current.revision
                or candidate.old_content_hash != current.content_hash
                or candidate.old_resolved_ref != current.resolved_ref
                or candidate.old_source != current.source
            ):
                raise ValueError("Skill 已在 check-update 后变化，候选过期")
            if not candidate.diff.changed:
                raise ValueError("Skill 候选与当前安装内容相同")
            return await self._replace_with_candidate(current, bundle)

    async def _replace_with_candidate(
        self,
        current: SkillRecord,
        bundle: SkillBundle,
    ) -> SkillRecord:
        target = self._package_directory(current.name)
        self.installer.validate_managed_path(self.installer.staging_root)
        self.installer.staging_root.mkdir(parents=True, exist_ok=True)
        self.installer.validate_managed_path(self.installer.staging_root)
        temporary = Path(tempfile.mkdtemp(
            prefix=f"update-{current.name}-",
            dir=self.installer.staging_root,
        ))
        replacement = temporary / "replacement" / current.name
        backup = temporary / "backup" / current.name
        backup.parent.mkdir(parents=True)
        cleanup_temporary = True
        try:
            write_skill_bundle(replacement, bundle)
            verified = self.package_reader.read(replacement)
            if verified.content_hash != bundle.content_hash:
                raise RuntimeError("Skill update staging hash 不一致")
            target.replace(backup)
            replacement.replace(target)
            proposed = SkillRecord(
                name=current.name,
                description=bundle.description,
                source=bundle.source,
                resolved_ref=bundle.resolved_ref,
                content_hash=bundle.content_hash,
                enabled=current.enabled,
                revision=current.revision,
                created_at=current.created_at,
            )
            try:
                return await self.registry.replace(proposed)
            except BaseException as registry_error:
                if target.exists():
                    shutil.rmtree(target)
                try:
                    backup.replace(target)
                except Exception as rollback_error:
                    cleanup_temporary = False
                    raise RuntimeError(
                        "Skill Registry 更新失败且包回滚失败；"
                        f"备份保留在 {backup}"
                    ) from rollback_error
                raise registry_error
        finally:
            if cleanup_temporary and temporary.exists():
                shutil.rmtree(temporary)

    async def remove(self, skill_id: str) -> SkillRecord:
        validate_skill_id(skill_id)
        async with self._management_lock:
            if self._has_active_turns():
                raise ValueError("活动 Turn 期间不允许卸载 Skill 包")
            record = await self.registry.get(skill_id)
            if record is None:
                raise KeyError(skill_id)
            target = self._package_directory(skill_id)
            if not target.is_dir():
                raise RuntimeError(f"已登记 Skill 包目录丢失: {skill_id}")
            self.installer.validate_managed_path(self.installer.staging_root)
            self.installer.staging_root.mkdir(parents=True, exist_ok=True)
            self.installer.validate_managed_path(self.installer.staging_root)
            temporary_parent = Path(tempfile.mkdtemp(
                prefix=f"remove-{skill_id}-",
                dir=self.installer.staging_root,
            ))
            held_package = temporary_parent / skill_id
            target.replace(held_package)
            cleanup_temporary = True
            try:
                removed = await self.registry.remove(skill_id)
            except BaseException as registry_error:
                try:
                    held_package.replace(target)
                except Exception as rollback_error:
                    cleanup_temporary = False
                    raise RuntimeError(
                        "Skill Registry 删除失败且包回滚失败；"
                        f"备份保留在 {held_package}"
                    ) from rollback_error
                raise registry_error
            finally:
                if cleanup_temporary and temporary_parent.exists():
                    shutil.rmtree(temporary_parent)
            return removed

    async def _validated_bundle(
        self,
        skill_id: str,
    ) -> tuple[SkillRecord, SkillBundle]:
        validate_skill_id(skill_id)
        record = await self.registry.get(skill_id)
        if record is None:
            raise KeyError(skill_id)
        package_directory = self._package_directory(skill_id)
        if package_directory.name != skill_id or not package_directory.is_dir():
            raise RuntimeError(f"已登记 Skill 包目录丢失: {skill_id}")
        bundle = self.package_reader.read(package_directory)
        if bundle.name != skill_id:
            raise RuntimeError(
                f"Skill 严格身份不一致: directory={skill_id}, "
                f"frontmatter={bundle.name}"
            )
        if bundle.description != record.description:
            raise RuntimeError(f"Skill Registry description 与包不一致: {skill_id}")
        if bundle.content_hash != record.content_hash:
            raise RuntimeError(f"Skill Registry hash 与包不一致: {skill_id}")
        return record, bundle

    def _package_directory(self, skill_id: str) -> Path:
        directory = (self.installer.packages_root / skill_id).resolve()
        if (
            not directory.is_relative_to(self.installer.packages_root.resolve())
            or not directory.is_relative_to(self.workspace.skills_root.resolve())
        ):
            raise RuntimeError("Skill package directory 越界")
        return directory

    @staticmethod
    def _catalog_text(records: tuple[SkillRecord, ...]) -> str:
        return "\n".join(
            f"- {record.name}: {record.description}"
            for record in sorted(records, key=lambda item: item.name)
        )
