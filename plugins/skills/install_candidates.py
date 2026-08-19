from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile

from plugins.skills.models import SkillBundle, SkillInstallCandidate
from plugins.skills.package import LocalSkillPackageReader, write_skill_bundle


class SkillInstallCandidateStore:
    def __init__(
        self,
        skills_root: Path,
        package_reader: LocalSkillPackageReader,
    ) -> None:
        self.skills_root = skills_root.resolve()
        self.root = self.skills_root / ".staging" / "install-candidates"
        self.package_reader = package_reader

    def freeze(self, bundle: SkillBundle) -> SkillInstallCandidate:
        candidate = SkillInstallCandidate(
            skill_id=bundle.name,
            description=bundle.description,
            source=bundle.source,
            resolved_ref=bundle.resolved_ref,
            content_hash=bundle.content_hash,
        )
        destination = self._directory(candidate.content_hash)
        if destination.exists():
            frozen, frozen_bundle = self.load(
                candidate.skill_id,
                candidate.content_hash,
            )
            if (
                frozen.skill_id != candidate.skill_id
                or frozen.description != candidate.description
                or frozen.source != candidate.source
                or frozen.resolved_ref != candidate.resolved_ref
                or frozen_bundle.content_hash != bundle.content_hash
            ):
                raise RuntimeError("install candidate hash 与已冻结内容冲突")
            return frozen

        safe_root = self._safe_root()
        safe_root.mkdir(parents=True, exist_ok=True)
        safe_root = self._safe_root()
        temporary = Path(tempfile.mkdtemp(
            prefix="install-candidate-",
            dir=safe_root,
        ))
        try:
            package = temporary / "package" / bundle.name
            write_skill_bundle(package, bundle)
            verified = self.package_reader.read(package)
            if verified.content_hash != bundle.content_hash:
                raise RuntimeError("install candidate staging hash 不一致")
            (temporary / "candidate.json").write_text(
                json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return candidate

    def load(
        self,
        skill_id: str,
        content_hash: str,
    ) -> tuple[SkillInstallCandidate, SkillBundle]:
        directory = self._directory(content_hash)
        metadata_path = directory / "candidate.json"
        if not metadata_path.is_file():
            raise KeyError(content_hash)
        candidate = SkillInstallCandidate.from_dict(json.loads(
            metadata_path.read_text(encoding="utf-8")
        ))
        if candidate.skill_id != skill_id or candidate.content_hash != content_hash:
            raise RuntimeError("install candidate 索引与内容身份不一致")
        bundle = self.package_reader.read(
            directory / "package" / candidate.skill_id
        )
        if bundle.content_hash != candidate.content_hash:
            raise RuntimeError("install candidate 冻结包 hash 已变化")
        return candidate, replace(
            bundle,
            source=candidate.source,
            resolved_ref=candidate.resolved_ref,
        )

    def _directory(self, content_hash: str) -> Path:
        if (
            len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
        ):
            raise ValueError("content_hash 必须是 64 位小写 SHA-256")
        return (self._safe_root() / content_hash).resolve()

    def _safe_root(self) -> Path:
        resolved = self.root.resolve()
        if not resolved.is_relative_to(self.skills_root):
            raise ValueError("Skill install candidate staging root 越界")
        return resolved
