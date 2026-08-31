from __future__ import annotations

from dataclasses import dataclass

from helperme.assistant.artifacts import (
    READ_ARTIFACT_SCHEMA,
    FileArtifactGateway,
    read_artifact_binding,
)
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.context.projection import (
    ModelContextProjector,
    ModelContextSettings,
)
from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.toolsets import ToolSurface, load_toolset_binding
from helperme.runtime import ToolBinding
from helperme.assistant.builtin_tools import (
    BuiltinToolRunner,
    build_builtin_tools,
)
from helperme.assistant.decision import bind_executor_tools
from helperme.assistant.mcp import McpToolsetAdapter
from helperme.assistant.management import ManagementDomain, ManagementSurface
from helperme.assistant.skills import SkillToolAdapter
from helperme.config import AssistantConfig
from helperme.paths import HelperMeHome, runtime_data_root
from helperme.mcp.composition import build_mcp
from helperme.skills.composition import build_skills
from helperme.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE
from helperme.skills.summarizer import LlmSkillDiffSummarizer


@dataclass(frozen=True, slots=True)
class AssistantAssembly:
    bindings: dict[str, ToolBinding]
    projector: ModelContextProjector
    builtin_tools: BuiltinToolRunner
    surface: ToolSurface
    mcp: object
    skills: object
    skill_tools: SkillToolAdapter
    control: AssistantControlPlane
    management: ManagementSurface


def _model_context_settings(config: AssistantConfig) -> ModelContextSettings:
    return ModelContextSettings(
        context_limit=config.model_context_limit,
        input_budget_ratio=config.input_budget_ratio,
    )


async def build_assistant_assembly(
    config: AssistantConfig,
    sink,
) -> AssistantAssembly:
    builtin_tools = await build_builtin_tools(config)
    settings = _model_context_settings(config)
    gateway = FileArtifactGateway(runtime_data_root())
    projector = ModelContextProjector(gateway=gateway, settings=settings)
    home = HelperMeHome.default()
    home.initialize()
    mcp = build_mcp(home)
    skills = build_skills(
        home,
        diff_summarizer=LlmSkillDiffSummarizer(
            config.llm,
            config.model_name,
        ),
    )
    control = AssistantControlPlane(
        specs=(
            mcp.install_proposal_spec,
            mcp.recovery_proposal_spec,
            mcp.update_proposal_spec,
            skills.install_proposal_spec,
            skills.enable_proposal_spec,
            skills.update_proposal_spec,
            skills.repair_proposal_spec,
        ),
        handlers=(
            mcp.install_approval_handler,
            mcp.recovery_approval_handler,
            mcp.update_approval_handler,
            skills.install_approval_handler,
            skills.enable_approval_handler,
            skills.update_approval_handler,
            skills.repair_approval_handler,
        ),
    )
    management = ManagementSurface(
        (
            ManagementDomain(
                "mcp",
                "MCP Server 的发现、诊断、安装、更新与修复",
                mcp.management_specs,
                (
                    mcp.install_proposal_spec.name,
                    mcp.recovery_proposal_spec.name,
                    mcp.update_proposal_spec.name,
                ),
            ),
            ManagementDomain(
                "skill",
                "Skill 的发现、检查、安装、启用、更新与修复",
                skills.management_specs,
                (
                    skills.install_proposal_spec.name,
                    skills.enable_proposal_spec.name,
                    skills.update_proposal_spec.name,
                    skills.repair_proposal_spec.name,
                ),
            ),
        ),
        gateway,
        settings,
    )
    skill_tools = SkillToolAdapter(skills, gateway, settings)
    surface = ToolSurface(
        providers=(McpToolsetAdapter(mcp),),
        base_schemas=[
            *builtin_tools.schemas,
            READ_ARTIFACT_SCHEMA,
        ],
        reserved_names=(
            *builtin_tools.names(),
            "read_artifact",
            DELIVER_TOOL_NAME,
            LOAD_SKILL,
            READ_SKILL_RESOURCE,
            *management.names(),
        ),
        gateway=gateway,
        settings=settings,
    )
    return AssistantAssembly(
        bindings={
            **bind_executor_tools(builtin_tools, gateway, settings),
            **read_artifact_binding(gateway),
            **deliver_binding(sink),
            **load_toolset_binding(surface),
            **skill_tools.bindings(),
            **management.bindings(),
        },
        projector=projector,
        builtin_tools=builtin_tools,
        surface=surface,
        mcp=mcp,
        skills=skills,
        skill_tools=skill_tools,
        control=control,
        management=management,
    )
