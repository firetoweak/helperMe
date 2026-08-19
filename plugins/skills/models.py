from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Literal
from typing import Any, cast


_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_skill_id(skill_id: str) -> str:
    if not isinstance(skill_id, str) or not _SKILL_ID_PATTERN.fullmatch(skill_id):
        raise ValueError(
            "Skill name 必须匹配 ^[a-z0-9][a-z0-9-]{0,63}$"
        )
    return skill_id


def validate_skill_description(description: str) -> str:
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Skill description 不能为空")
    if description != description.strip():
        raise ValueError("Skill description 不能包含首尾空白")
    if "\n" in description or "\r" in description:
        raise ValueError("Skill description 必须是单行文本")
    if len(description) > 1_000:
        raise ValueError("Skill description 超出 1000 字符限制")
    return description


SkillSourceKind = Literal["local", "github", "url"]


@dataclass(frozen=True)
class SkillSourceRef:
    kind: SkillSourceKind
    locator: str
    requested_ref: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"local", "github", "url"}:
            raise ValueError(f"不支持的 Skill source kind: {self.kind}")
        if not self.locator.strip():
            raise ValueError("Skill source locator 不能为空")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "locator": self.locator,
            "requested_ref": self.requested_ref,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "SkillSourceRef":
        requested_ref = value.get("requested_ref")
        return cls(
            kind=cast(SkillSourceKind, str(value["kind"])),
            locator=str(value["locator"]),
            requested_ref=(
                str(requested_ref) if requested_ref is not None else None
            ),
        )


@dataclass(frozen=True)
class SkillFile:
    relative_path: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class SkillBundle:
    name: str
    description: str
    main_instructions: str
    source: SkillSourceRef
    resolved_ref: str
    content_hash: str
    files: tuple[SkillFile, ...]

    def __post_init__(self) -> None:
        validate_skill_id(self.name)
        validate_skill_description(self.description)
        if not self.resolved_ref.strip():
            raise ValueError("Skill resolved_ref 不能为空")
        if not self.content_hash.strip():
            raise ValueError("Skill content_hash 不能为空")
        paths = [item.relative_path for item in self.files]
        if "SKILL.md" not in paths:
            raise ValueError("SkillBundle 必须包含 SKILL.md")
        if len(paths) != len(set(paths)):
            raise ValueError("SkillBundle 不能包含重复路径")


@dataclass(frozen=True)
class SkillPackageLimits:
    max_files: int = 512
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 20 * 1024 * 1024
    max_main_instruction_chars: int = 100_000

    def __post_init__(self) -> None:
        if min(
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_main_instruction_chars,
        ) <= 0:
            raise ValueError("Skill package limits 必须大于 0")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return utc_now()


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    source: SkillSourceRef
    resolved_ref: str
    content_hash: str
    enabled: bool = False
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_skill_id(self.name)
        validate_skill_description(self.description)
        if not self.resolved_ref.strip():
            raise ValueError("Skill resolved_ref 不能为空")
        if not self.content_hash.strip():
            raise ValueError("Skill content_hash 不能为空")
        if self.revision < 1:
            raise ValueError("Skill revision 必须大于 0")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source.to_dict(),
            "resolved_ref": self.resolved_ref,
            "content_hash": self.content_hash,
            "enabled": self.enabled,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "SkillRecord":
        raw_source = value.get("source")
        if not isinstance(raw_source, dict):
            raise ValueError("Skill record source 必须是 object")
        return cls(
            name=str(value["name"]),
            description=str(value["description"]),
            source=SkillSourceRef.from_dict(raw_source),
            resolved_ref=str(value["resolved_ref"]),
            content_hash=str(value["content_hash"]),
            enabled=bool(value.get("enabled", False)),
            revision=int(value.get("revision", 1)),
            created_at=_parse_datetime(value.get("created_at")),
            updated_at=_parse_datetime(value.get("updated_at")),
        )


@dataclass(frozen=True)
class SkillManifestDiff:
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "added": list(self.added),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
        }
        categories: dict[str, dict[str, list[str]]] = {}
        for change, paths in (
            ("added", self.added),
            ("modified", self.modified),
            ("deleted", self.deleted),
        ):
            for path in paths:
                category = _skill_file_category(path)
                categories.setdefault(category, {
                    "added": [],
                    "modified": [],
                    "deleted": [],
                })[change].append(path)
        payload["skill_md_changed"] = "SKILL.md" in {
            *self.added,
            *self.modified,
            *self.deleted,
        }
        payload["categories"] = categories
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillManifestDiff":
        return cls(
            added=tuple(str(item) for item in value.get("added", ())),
            modified=tuple(str(item) for item in value.get("modified", ())),
            deleted=tuple(str(item) for item in value.get("deleted", ())),
        )


def _skill_file_category(path: str) -> str:
    if path == "SKILL.md":
        return "instructions"
    prefix = path.split("/", 1)[0]
    if prefix in {"references", "scripts", "templates", "assets"}:
        return prefix
    return "other"


@dataclass(frozen=True)
class SkillUpdateCandidate:
    skill_id: str
    old_revision: int
    old_resolved_ref: str
    old_content_hash: str
    old_source: SkillSourceRef
    new_resolved_ref: str
    candidate_hash: str
    source: SkillSourceRef
    operation: Literal["update", "replace"]
    diff: SkillManifestDiff
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_skill_id(self.skill_id)
        if self.old_revision < 1:
            raise ValueError("candidate old_revision 必须大于 0")
        for label, value in (
            ("old_resolved_ref", self.old_resolved_ref),
            ("old_content_hash", self.old_content_hash),
            ("new_resolved_ref", self.new_resolved_ref),
            ("candidate_hash", self.candidate_hash),
        ):
            if not value.strip():
                raise ValueError(f"candidate {label} 不能为空")
        if self.operation not in {"update", "replace"}:
            raise ValueError("candidate operation 必须是 update|replace")

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "old_revision": self.old_revision,
            "old_resolved_ref": self.old_resolved_ref,
            "old_content_hash": self.old_content_hash,
            "old_source": self.old_source.to_dict(),
            "new_resolved_ref": self.new_resolved_ref,
            "candidate_hash": self.candidate_hash,
            "source": self.source.to_dict(),
            "operation": self.operation,
            "diff": self.diff.to_dict(),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillUpdateCandidate":
        source = value.get("source")
        old_source = value.get("old_source")
        diff = value.get("diff")
        if (
            not isinstance(source, dict)
            or not isinstance(old_source, dict)
            or not isinstance(diff, dict)
        ):
            raise ValueError("Skill candidate source/diff 格式无效")
        return cls(
            skill_id=str(value["skill_id"]),
            old_revision=int(value["old_revision"]),
            old_resolved_ref=str(value["old_resolved_ref"]),
            old_content_hash=str(value["old_content_hash"]),
            old_source=SkillSourceRef.from_dict(old_source),
            new_resolved_ref=str(value["new_resolved_ref"]),
            candidate_hash=str(value["candidate_hash"]),
            source=SkillSourceRef.from_dict(source),
            operation=cast(
                Literal["update", "replace"],
                str(value["operation"]),
            ),
            diff=SkillManifestDiff.from_dict(diff),
            created_at=_parse_datetime(value.get("created_at")),
        )


@dataclass(frozen=True)
class SkillUpdateReport:
    candidate: SkillUpdateCandidate
    semantic_summary: str | None = None
    summary_error: str | None = None

    def __post_init__(self) -> None:
        if self.semantic_summary is None and self.summary_error is None:
            raise ValueError("Skill update report 必须包含概括或明确错误")
        if self.semantic_summary is not None and not self.semantic_summary.strip():
            raise ValueError("semantic_summary 不能为空")
        if self.summary_error is not None and not self.summary_error.strip():
            raise ValueError("summary_error 不能为空")

    def to_dict(self) -> dict[str, object]:
        return {
            "semantic_summary": self.semantic_summary,
            "summary_error": self.summary_error,
            "candidate": self.candidate.to_dict(),
        }


@dataclass(frozen=True)
class SkillInstallCandidate:
    skill_id: str
    description: str
    source: SkillSourceRef
    resolved_ref: str
    content_hash: str
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_skill_id(self.skill_id)
        if not self.description.strip():
            raise ValueError("install candidate description 不能为空")
        if not self.resolved_ref.strip() or not self.content_hash.strip():
            raise ValueError("install candidate ref/hash 不能为空")

    def to_dict(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id,
            "description": self.description,
            "source": self.source.to_dict(),
            "resolved_ref": self.resolved_ref,
            "content_hash": self.content_hash,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkillInstallCandidate":
        source = value.get("source")
        if not isinstance(source, dict):
            raise ValueError("install candidate source 格式无效")
        return cls(
            skill_id=str(value["skill_id"]),
            description=str(value["description"]),
            source=SkillSourceRef.from_dict(source),
            resolved_ref=str(value["resolved_ref"]),
            content_hash=str(value["content_hash"]),
            created_at=_parse_datetime(value.get("created_at")),
        )
