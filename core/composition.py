from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Mapping

from core.environment import (
    EnvironmentSelection,
    FilesystemAccessMode,
    LocalEnvironmentProvider,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
    discover_host_roots,
)
from core.agent_workspace import AgentWorkspace
from core.agent_application import AgentApplication, DEFAULT_MAX_STEPS
from core.model_call.client import LLMClient
from core.prompt import DEFAULT_AGENT_PROMPT
from core.runtime_modes import (
    PlainMode,
    TurnMode,
    RuntimeMode,
    RuntimeModeRouter,
)
from core.todos import TodoMode
from core.session import SessionRuntime
from core.tools_runtime.turn_runtime import TurnRuntime
from core.tools_runtime.turn_progress import TurnProgressSink
from core.context import (
    ContextBudget,
    ContextManager,
    ContextPreparationService,
    LLMContextSummaryGenerator,
    MicroCompactionConfig,
    MicroCompactionPolicy,
    ModelBudgetConfig,
    TiktokenTokenEstimator,
)
from core.model_call.service import ModelCallService
from core.runtime_artifacts import (
    FileArtifactDrawers,
    ToolResultExternalizer,
    ToolResultLimit,
)
from core.tool_registry import BUILTIN_TOOL_REGISTRY
from core.tool_registry import ToolSpec
from core.approval import ApprovalActionRegistry
from core.tools_runtime.progressive_toolsets import ToolsetProvider
from core.tools_runtime.tools_executor import ToolsExecutor
from tools.artifact_read import create_read_artifact_spec
from tools import create_environment_tool_specs
from tools.powershell_runner import PowerShellCommandRunner

# 无状态内建工具通过导入注册；Environment 工具在 Turn 中绑定。
import tools  # noqa: F401


def create_agent_application(
    model: str,
    model_context_limit: int,
    agent_workspace: AgentWorkspace,
    workspace_roots: Mapping[str, Path],
    input_budget_ratio: float = 0.75,
    runtime_mode: RuntimeMode | None = None,
    recent_protection_tokens: int = 10_000,
    llm_client: LLMClient | None = None,
    progress_sink: TurnProgressSink | None = None,
    filesystem_access_mode: FilesystemAccessMode = (
        FilesystemAccessMode.SCOPED
    ),
    default_max_steps: int = DEFAULT_MAX_STEPS,
    application_resources: tuple[
        AbstractAsyncContextManager[Any], ...
    ] = (),
    additional_tool_specs: tuple[ToolSpec, ...] = (),
    default_toolset_provider: ToolsetProvider | None = None,
    approval_actions: ApprovalActionRegistry | None = None,
) -> AgentApplication:
    if not model or not model.strip():
        raise ValueError("model 不能为空")
    effective_workspace_roots = dict(workspace_roots)
    if filesystem_access_mode is FilesystemAccessMode.HOST:
        host_roots = discover_host_roots()
        host_root_map = {root.root_id: root.path for root in host_roots}
        duplicated_names = (
            effective_workspace_roots.keys() & host_root_map.keys()
        )
        if duplicated_names:
            raise ValueError(
                "显式 workspace root 与 Host root 名称冲突: "
                f"{sorted(duplicated_names)}"
            )
        effective_workspace_roots.update(host_root_map)

    task_root_ids = set(workspace_roots)
    workspace_view = WorkspaceViewSnapshot(tuple(
        RootBinding(
            root_id=name,
            scope=(
                WorkspaceScope.TASK
                if name in task_root_ids
                else WorkspaceScope.HOST
            ),
            path=root,
        )
        for name, root in effective_workspace_roots.items()
    ))
    configured_workspace_paths = [
        root.resolve()
        for root in workspace_roots.values()
    ]
    if any(
        agent_workspace.root.is_relative_to(workspace_root)
        or workspace_root.is_relative_to(agent_workspace.root)
        for workspace_root in configured_workspace_paths
    ):
        raise ValueError("Agent Workspace 必须与用户 Workspace 相互独立")
    agent_workspace.initialize()

    application_tool_registry = BUILTIN_TOOL_REGISTRY.clone()
    for spec in additional_tool_specs:
        application_tool_registry.register(spec)
    command_runner = PowerShellCommandRunner()
    environment_provider = LocalEnvironmentProvider(
        command_runner,
        shell_path=command_runner.executable,
    )
    default_environment_selection = EnvironmentSelection(
        environment_id=environment_provider.environment_id,
        workspace_view=workspace_view,
        cwd=str(next(iter(workspace_roots.values())).resolve()),
    )

    owns_llm_client = llm_client is None
    if owns_llm_client:
        llm_client = LLMClient()
    context_budget = ContextBudget(
        estimator=TiktokenTokenEstimator(),
        config=ModelBudgetConfig(
            context_limit=model_context_limit,
            input_ratio=input_budget_ratio,
        ),
    )
    model_calls = ModelCallService(
        llm_client=llm_client,
        context_budget=context_budget,
    )
    result_limit = ToolResultLimit()
    context_manager = ContextManager()
    summary_generator = LLMContextSummaryGenerator(model_calls, model)
    artifact_drawers = FileArtifactDrawers(agent_workspace.sessions_root)
    mode_configuration = (
        {"runtime_mode": runtime_mode}
        if runtime_mode is not None
        else {
            "mode_router": RuntimeModeRouter(),
            "runtime_modes": {
                TurnMode.PLAIN: PlainMode(),
                TurnMode.TODO: TodoMode(),
            },
        }
    )

    def create_session_turn_runtime(session_id: str) -> TurnRuntime:
        artifact_store = artifact_drawers.for_session(session_id)
        tool_registry = application_tool_registry.clone()
        tool_registry.register(create_read_artifact_spec(artifact_store))
        context_preparation = ContextPreparationService(
            context_manager=context_manager,
            micro_compaction_policy=MicroCompactionPolicy(
                context_manager=context_manager,
                context_budget=context_budget,
                config=MicroCompactionConfig(
                    recent_protection_tokens=recent_protection_tokens,
                ),
                artifact_store=artifact_store,
            ),
            context_budget=context_budget,
            summary_generator=summary_generator,
        )
        return TurnRuntime(
            model_calls=model_calls,
            model=model,
            context_preparation=context_preparation,
            tools_executor=ToolsExecutor(tool_registry),
            tool_result_externalizer=ToolResultExternalizer(
                artifact_store,
                result_limit,
            ),
            progress_sink=progress_sink,
            environment_tool_factory=lambda binding: (
                create_environment_tool_specs(binding)
            ),
            **mode_configuration,
        )

    session_runtime = SessionRuntime(
        turn_runtime_factory=create_session_turn_runtime,
        delete_session_resources=artifact_drawers.delete,
        default_toolset_provider=default_toolset_provider,
        environment_provider=environment_provider,
        default_environment_selection=default_environment_selection,
    )
    return AgentApplication(
        session_runtime=session_runtime,
        system_prompt=DEFAULT_AGENT_PROMPT,
        default_max_steps=default_max_steps,
        resources=(llm_client, *application_resources)
        if owns_llm_client
        else application_resources,
        approval_actions=approval_actions,
    )
