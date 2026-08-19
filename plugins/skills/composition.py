from __future__ import annotations

from dataclasses import dataclass

from core.agent_workspace import AgentWorkspace
from plugins.skills.application import SkillApplicationService
from plugins.skills.summarizer import SkillDiffSummarizer
from plugins.skills.approval import (
    SkillEnableApprovalHandler,
    SkillInstallApprovalHandler,
    create_skill_enable_proposal_spec,
    create_skill_install_proposal_spec,
)
from plugins.skills.management_tools import create_skill_management_specs
from core.tool_registry import ToolSpec


@dataclass(frozen=True)
class SkillPlugin:
    service: SkillApplicationService
    install_proposal_spec: ToolSpec
    install_approval_handler: SkillInstallApprovalHandler
    management_specs: tuple[ToolSpec, ...]
    enable_proposal_spec: ToolSpec
    enable_approval_handler: SkillEnableApprovalHandler

    @property
    def skill_provider(self):
        return self.service.skill_provider


def create_skill_plugin(
    agent_workspace: AgentWorkspace,
    *,
    diff_summarizer: SkillDiffSummarizer | None = None,
) -> SkillPlugin:
    service = SkillApplicationService(
        agent_workspace,
        diff_summarizer=diff_summarizer,
    )
    return SkillPlugin(
        service=service,
        install_proposal_spec=create_skill_install_proposal_spec(service),
        install_approval_handler=SkillInstallApprovalHandler(service),
        management_specs=create_skill_management_specs(service),
        enable_proposal_spec=create_skill_enable_proposal_spec(service),
        enable_approval_handler=SkillEnableApprovalHandler(service),
    )
