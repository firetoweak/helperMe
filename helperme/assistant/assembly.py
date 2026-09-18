from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from helperme.assistant.artifacts import (
    READ_ARTIFACT_SCHEMA,
    FileArtifactGateway,
    read_artifact_binding,
)
from helperme.assistant.attachments import (
    AttachmentGateway,
    READ_IMAGE_SCHEMA,
    read_image_binding,
)
from helperme.assistant.compact.core import CompactContext, CompactBoundary, READ, SUBMIT
from helperme.assistant.loop_guard import LoopGuard
from helperme.assistant.loop_guard_strategies import ConsecutiveActions
from helperme.assistant.delivery import (
    DELIVER_TOOL_NAME,
    PreviewEmitter,
    deliver_binding,
    emit_delivery,
)
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
from helperme.assistant.subagent.subagent import DELEGATE, REPORT, SubAgentHost
from helperme.assistant.toolsets import ToolSurface, load_toolset_binding
from helperme.runtime import AgentRuntime, ToolBinding
from helperme.assistant.builtin_tools import build_builtin_tools
from helperme.sandbox.registry import WorkspaceRecord
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
    compact: CompactBoundary | None = None


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
    session_id: str,
    workspace: WorkspaceRecord,
    context_usage_sink: Callable[[str, int, int], None] | None = None,
    subagent_activity_sink: Callable[[str, bool], None] | None = None,
    tool_progress_sink=None,
    authorization_required_sink=None,
    preview_sink=None,
    thinking_sink=None,
    session_failed_sink: Callable[[str, str], Awaitable[None] | None] | None = None,
    scheduler_factory=SessionScheduler,
    session_transport=None,
    home: HelperMeHome | None = None,
) -> AssistantAssembly:
    builtin_tools = await build_builtin_tools(workspace)
    settings = _model_context_settings(config)
    sessions_root = runtime_data_root() if home is None else home.runtime_sessions_root
    gateway = FileArtifactGateway(sessions_root)
    attachment_gateway = AttachmentGateway(sessions_root)
    attachments = attachment_gateway.for_session(session_id)
    projector = ModelContextProjector(
        gateway=gateway,
        attachments=attachment_gateway,
        settings=settings,
    )
    home = HelperMeHome.default() if home is None else home
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
    subagents = SubAgentHost(subagent_activity_sink)
    preview = PreviewEmitter(preview_sink, thinking_sink)
    delivery_sink = subagents.routed_sink(sink)

    async def notify(session_id: str, text: str) -> None:
        await emit_delivery(
            delivery_sink,
            session_id,
            f"notification-{uuid4().hex}",
            text,
        )

    async def report_session_failed(session_id: str, text: str) -> None:
        if subagents.is_subagent(session_id) or session_failed_sink is None:
            return
        observed = session_failed_sink(session_id, text)
        if isinstance(observed, Awaitable):
            await observed

    surface = ToolSurface(
        providers=(McpToolsetAdapter(mcp, attachments),),
        base_schemas=[
            *builtin_tools.schemas,
            READ_ARTIFACT_SCHEMA,
            READ_IMAGE_SCHEMA,
        ],
        reserved_names=(
            *builtin_tools.names(),
            "read_artifact",
            "read_image",
            DELIVER_TOOL_NAME,
            LOAD_SKILL,
            READ_SKILL_RESOURCE,
            DELEGATE,
            REPORT,
            READ,
            SUBMIT,
            *management.names(),
            *(operation.name for operation in operations),
        ),
        gateway=gateway,
        settings=settings,
    )
    compact_context = CompactContext(
        session_id,
        await journal.snapshot(session_id),
        projector,
        session_transport,
    )
    bind_reader = getattr(config.llm, "bind_attachment_reader", None)
    if bind_reader is not None:
        bind_reader(
            compact_context.read_attachment
            if compact_context.is_reader
            else attachments.read
        )
    bindings = {
        **bind_executor_tools(builtin_tools, gateway, settings),
        **read_artifact_binding(gateway),
        **read_image_binding(journal, attachments),
        **deliver_binding(delivery_sink, preview),
        **load_toolset_binding(surface),
        **skill_tools.bindings(),
        **management.bindings(),
        **subagents.bindings(),
        **compact_context.bindings(),
    }
    bindings = _with_tool_progress(bindings, tool_progress_sink)
    decision = JournalBackedLlmDecisionMaker(
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
        compact=compact_context,
        loop_guard=LoopGuard((ConsecutiveActions(config.loop_guard_repeat_threshold),)),
        preview=preview,
    )
    runtime = AgentRuntime(journal, decision, bindings)
    surface.attach(runtime)
    compact_context.runtime = runtime
    scheduler = scheduler_factory(
        runtime,
        session_id,
        control=control,
        # 失败与控制面提示同样是子 Session 的对外输出，一样不外露：
        # 用户该看到的是父转述后的判断，不是一条不知来处的裸错误。
        notify=notify,
        on_quiesced=subagents.on_quiesced,
        on_failed=subagents.on_failed,
        session_failed=report_session_failed,
        preview=preview,
    )
    compact = None
    if session_transport is not None:
        compact = CompactBoundary(
            runtime, decision, compact_context, config, control, session_transport
        )
        compact.scheduler = scheduler

        scheduler.propagate_failures = compact_context.is_reader
    from helperme.assistant.catalog import sync_catalog

    subagents.attach(runtime, session_transport)
    sessions = AssistantSessions(
        runtime,
        surface,
        scheduler,
        control=control,
        management=management,
        subagents=subagents,
        meta_root=sessions_root,
    )

    async def before_advance():
        if sessions.is_paused(session_id):
            return False
        if not compact_context.is_reader and not subagents.is_subagent(session_id):
            if not (await runtime.state(session_id)).waiting_command_ids:
                await sync_catalog(runtime, session_id, surface, skill_tools, management)
        return True if compact is None else await compact.before_advance()

    scheduler.before_advance = before_advance
    scheduler.auto_authorize = sessions.is_auto_authorized
    scheduler.authorization_required = authorization_required_sink
    return AssistantAssembly(
        runtime=runtime,
        scheduler=scheduler,
        sessions=sessions,
        compact=compact,
        bindings=bindings,
        surface=surface,
        mcp=mcp,
        skills=skills,
        control=control,
        subagents=subagents,
    )


def _with_tool_progress(bindings, sink):
    if sink is None:
        return bindings
    projected = {}
    for name, binding in bindings.items():
        if name == DELIVER_TOOL_NAME:
            projected[name] = binding
            continue

        async def handler(context, arguments, _name=name, _handler=binding.handler):
            sink(context.session_id, "start", context.command_id, _name, arguments)
            try:
                result = await _handler(context, arguments)
            except BaseException:
                sink(context.session_id, "fail", context.command_id, _name, None)
                raise
            sink(context.session_id, "finish", context.command_id, _name, result)
            return result

        projected[name] = ToolBinding(
            handler,
            decision_on_outcome=binding.decision_on_outcome,
            requires_authorization=binding.requires_authorization,
        )
    return projected
