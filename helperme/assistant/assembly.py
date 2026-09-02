from __future__ import annotations

from collections.abc import Callable
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
from helperme.assistant.decision import (
    JournalBackedLlmDecisionMaker,
    bind_executor_tools,
)
from helperme.assistant.runner import SessionScheduler
from helperme.assistant.sessions import AssistantSessions
from helperme.assistant.subagent import DELEGATE, REPORT, SubAgentHost
from helperme.assistant.toolsets import ToolSurface, load_toolset_binding
from helperme.runtime import AgentRuntime, ToolBinding
from helperme.assistant.builtin_tools import build_builtin_tools
from helperme.assistant.mcp import McpToolsetAdapter
from helperme.assistant.management import ManagementDomain, ManagementSurface
from helperme.assistant.skills import SkillToolAdapter
from helperme.config import AssistantConfig
from helperme.paths import HelperMeHome, runtime_data_root
from helperme.mcp.composition import McpAssembly, build_mcp
from helperme.skills.composition import SkillAssembly, build_skills
from helperme.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE
from helperme.skills.summarizer import LlmSkillDiffSummarizer


@dataclass(frozen=True, slots=True)
class AssistantAssembly:
    runtime: AgentRuntime
    scheduler: SessionScheduler
    sessions: AssistantSessions
    bindings: dict[str, ToolBinding]
    surface: ToolSurface
    mcp: McpAssembly
    skills: SkillAssembly
    control: AssistantControlPlane
    subagents: SubAgentHost


def _model_context_settings(config: AssistantConfig) -> ModelContextSettings:
    return ModelContextSettings(
        context_limit=config.model_context_limit,
        input_budget_ratio=config.input_budget_ratio,
    )


async def build_assistant_assembly(
    config: AssistantConfig,
    sink,
    journal,
    *,
    context_usage_sink: Callable[[str, int, int], None] | None = None,
    scheduler_factory=SessionScheduler,
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
    operations = (*mcp.control_operations, *skills.control_operations)
    control = AssistantControlPlane(operations)
    management = ManagementSurface(
        (
            ManagementDomain(
                "mcp",
                "MCP Server 的发现、诊断、安装、更新与修复",
                mcp.management_specs,
                mcp.control_operations,
            ),
            ManagementDomain(
                "skill",
                "Skill 的发现、检查、安装、启用、更新与修复",
                skills.management_specs,
                skills.control_operations,
            ),
        ),
        gateway,
        settings,
    )
    skill_tools = SkillToolAdapter(skills, gateway, settings)
    subagents = SubAgentHost()
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
            DELEGATE,
            REPORT,
            *management.names(),
            *(operation.name for operation in operations),
        ),
        gateway=gateway,
        settings=settings,
    )
    bindings = {
        **bind_executor_tools(builtin_tools, gateway, settings),
        **read_artifact_binding(gateway),
        **deliver_binding(subagents.routed_sink(sink)),
        **load_toolset_binding(surface),
        **skill_tools.bindings(),
        **management.bindings(),
        **subagents.bindings(),
    }
    runtime = AgentRuntime(
        journal,
        JournalBackedLlmDecisionMaker(
            journal,
            config.llm,
            config.model_name,
            surface=surface,
            skill_tools=skill_tools,
            projector=projector,
            control=control,
            management=management,
            context_usage_sink=context_usage_sink,
            subagents=subagents,
        ),
        bindings,
    )
    surface.attach(runtime)
    scheduler = scheduler_factory(
        runtime,
        control=control,
        # 失败与控制面提示同样是子 Session 的对外输出，一样不外露：
        # 用户该看到的是父转述后的判断，不是一条不知来处的裸错误。
        notify=subagents.routed_sink(sink),
        on_quiesced=subagents.on_quiesced,
        on_failed=subagents.on_failed,
    )
    subagents.attach(runtime, scheduler)
    sessions = AssistantSessions(
        runtime,
        surface,
        scheduler,
        control=control,
        management=management,
        subagents=subagents,
    )
    return AssistantAssembly(
        runtime=runtime,
        scheduler=scheduler,
        sessions=sessions,
        bindings=bindings,
        surface=surface,
        mcp=mcp,
        skills=skills,
        control=control,
        subagents=subagents,
    )
