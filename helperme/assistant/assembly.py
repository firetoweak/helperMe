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
from helperme.assistant.toolsets import ToolSurface, load_toolset_binding
from helperme.runtime import ToolBinding
from helperme.assistant.builtin_tools import (
    BuiltinToolRunner,
    build_builtin_tools,
)
from helperme.assistant.decision import bind_executor_tools
from helperme.assistant.mcp import McpToolsetAdapter
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
    model_tools: list[dict[str, object]]
    projector: ModelContextProjector
    builtin_tools: BuiltinToolRunner
    surface: ToolSurface
    mcp: object
    skills: object
    skill_tools: SkillToolAdapter


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
    skill_tools = SkillToolAdapter(skills, gateway, settings)
    surface = ToolSurface(
        providers=(McpToolsetAdapter(mcp),),
        base_schemas=[*builtin_tools.schemas, READ_ARTIFACT_SCHEMA],
        reserved_names=(
            *builtin_tools.names(),
            "read_artifact",
            DELIVER_TOOL_NAME,
            LOAD_SKILL,
            READ_SKILL_RESOURCE,
        ),
        gateway=gateway,
        settings=settings,
    )
    model_tools = [*builtin_tools.schemas, READ_ARTIFACT_SCHEMA]
    return AssistantAssembly(
        bindings={
            **bind_executor_tools(builtin_tools, gateway, settings),
            **read_artifact_binding(gateway),
            **deliver_binding(sink),
            **load_toolset_binding(surface),
            **skill_tools.bindings(),
        },
        model_tools=model_tools,
        projector=projector,
        builtin_tools=builtin_tools,
        surface=surface,
        mcp=mcp,
        skills=skills,
        skill_tools=skill_tools,
    )
