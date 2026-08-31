from __future__ import annotations

from dataclasses import dataclass

from helperme.paths import HelperMeHome
from helperme.skills.application import SkillApplicationService
from helperme.skills.summarizer import SkillDiffSummarizer
from helperme.skills.approval import (
    SkillEnableApprovalHandler,
    SkillInstallApprovalHandler,
    SkillRepairApprovalHandler,
    SkillUpdateApprovalHandler,
    create_skill_enable_proposal_spec,
    create_skill_install_proposal_spec,
    create_skill_repair_proposal_spec,
    create_skill_update_proposal_spec,
)
from helperme.skills.management_tools import create_skill_management_specs
from helperme.skills.runtime import SkillToolCatalog
from helperme.tools.control import ControlOperation
from helperme.tools.spec import ToolSpec


@dataclass(frozen=True)
class SkillAssembly:
    service: SkillApplicationService
    management_specs: tuple[ToolSpec, ...]
    control_operations: tuple[ControlOperation, ...]

    @property
    def tool_catalog(self) -> SkillToolCatalog:
        return self.service.tool_catalog


def build_skills(
    home: HelperMeHome,
    *,
    diff_summarizer: SkillDiffSummarizer | None = None,
) -> SkillAssembly:
    service = SkillApplicationService(
        home,
        diff_summarizer=diff_summarizer,
    )
    control_operations = (
        ControlOperation(
            "skill",
            create_skill_install_proposal_spec(service),
            SkillInstallApprovalHandler(service),
        ),
        ControlOperation(
            "skill",
            create_skill_enable_proposal_spec(service),
            SkillEnableApprovalHandler(service),
        ),
        ControlOperation(
            "skill",
            create_skill_update_proposal_spec(service),
            SkillUpdateApprovalHandler(service),
        ),
        ControlOperation(
            "skill",
            create_skill_repair_proposal_spec(service),
            SkillRepairApprovalHandler(service),
        ),
    )
    return SkillAssembly(
        service=service,
        management_specs=create_skill_management_specs(service),
        control_operations=control_operations,
    )
