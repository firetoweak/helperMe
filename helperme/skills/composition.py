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
from helperme.tools.spec import ToolSpec


@dataclass(frozen=True)
class SkillAssembly:
    service: SkillApplicationService
    install_proposal_spec: ToolSpec
    install_approval_handler: SkillInstallApprovalHandler
    management_specs: tuple[ToolSpec, ...]
    enable_proposal_spec: ToolSpec
    enable_approval_handler: SkillEnableApprovalHandler
    update_proposal_spec: ToolSpec
    update_approval_handler: SkillUpdateApprovalHandler
    repair_proposal_spec: ToolSpec
    repair_approval_handler: SkillRepairApprovalHandler

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
    return SkillAssembly(
        service=service,
        install_proposal_spec=create_skill_install_proposal_spec(service),
        install_approval_handler=SkillInstallApprovalHandler(service),
        management_specs=create_skill_management_specs(service),
        enable_proposal_spec=create_skill_enable_proposal_spec(service),
        enable_approval_handler=SkillEnableApprovalHandler(service),
        update_proposal_spec=create_skill_update_proposal_spec(service),
        update_approval_handler=SkillUpdateApprovalHandler(service),
        repair_proposal_spec=create_skill_repair_proposal_spec(service),
        repair_approval_handler=SkillRepairApprovalHandler(service),
    )
