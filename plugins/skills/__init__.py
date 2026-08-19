"""Skill 安装、管理与渐进加载能力。"""

from plugins.skills.models import (
    SkillBundle,
    SkillFile,
    SkillPackageLimits,
    SkillRecord,
    SkillSourceRef,
)
from plugins.skills.installer import LocalSkillInstaller
from plugins.skills.application import SkillApplicationService, SkillInspection
from plugins.skills.console import SkillCommandError, SkillConsoleAdapter
from plugins.skills.package import LocalSkillPackageReader, SkillPackageError
from plugins.skills.registry import SkillRegistry
from plugins.skills.runtime import (
    LOAD_SKILL,
    READ_SKILL_RESOURCE,
    SkillRuntimeCapability,
    SkillRuntimeError,
)
from plugins.skills.sources import SkillSourceError, SkillSourceRouter
from plugins.skills.summarizer import LlmSkillDiffSummarizer, SkillDiffSummarizer
from plugins.skills.approval import (
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
    "SkillRuntimeCapability",
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
