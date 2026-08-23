"""Skill 安装、管理与渐进加载能力。"""

from helperme.skills.models import (
    SkillBundle,
    SkillFile,
    SkillPackageLimits,
    SkillRecord,
    SkillSourceRef,
)
from helperme.skills.installer import LocalSkillInstaller
from helperme.skills.application import SkillApplicationService, SkillInspection
from helperme.skills.console import SkillCommandError, SkillConsoleAdapter
from helperme.skills.package import LocalSkillPackageReader, SkillPackageError
from helperme.skills.registry import SkillRegistry
from helperme.skills.runtime import (
    LOAD_SKILL,
    READ_SKILL_RESOURCE,
    SkillToolCatalog,
    SkillRuntimeError,
)
from helperme.skills.sources import SkillSourceError, SkillSourceRouter
from helperme.skills.summarizer import LlmSkillDiffSummarizer, SkillDiffSummarizer
from helperme.skills.approval import (
    PROPOSE_SKILL_ENABLE,
    PROPOSE_SKILL_INSTALL,
    SkillEnableApprovalHandler,
    SkillInstallApprovalHandler,
    create_skill_install_proposal_spec,
)

__all__ = [
    "LocalSkillPackageReader",
    "LOAD_SKILL",
    "READ_SKILL_RESOURCE",
    "SkillToolCatalog",
    "SkillRuntimeError",
    "LocalSkillInstaller",
    "SkillApplicationService",
    "SkillBundle",
    "SkillFile",
    "SkillInspection",
    "SkillCommandError",
    "SkillConsoleAdapter",
    "SkillPackageError",
    "SkillPackageLimits",
    "SkillRecord",
    "SkillRegistry",
    "SkillSourceRef",
    "SkillSourceError",
    "SkillSourceRouter",
    "SkillDiffSummarizer",
    "LlmSkillDiffSummarizer",
    "PROPOSE_SKILL_INSTALL",
    "PROPOSE_SKILL_ENABLE",
    "SkillEnableApprovalHandler",
    "SkillInstallApprovalHandler",
    "create_skill_install_proposal_spec",
]
