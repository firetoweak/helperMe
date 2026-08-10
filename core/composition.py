from __future__ import annotations

from pathlib import Path
from typing import Mapping

from core.agent_application import AgentApplication
from core.model_call.client import LLMClient
from core.prompt import DEFAULT_AGENT_PROMPT
from core.runtime_modes import (
    PlainMode,
    RunMode,
    RuntimeMode,
    RuntimeModeRouter,
)
from core.todos import TodoMode
from core.goals import (
    GoalApplicationService,
    GoalCommandBufferRegistry,
    InMemoryGoalStore,
)
from core.session import SessionRuntime
from core.tools_runtime.run_runtime import RunRuntime
from core.tools_runtime.run_progress import RunProgressSink
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
from core.tools_runtime.tools_executor import ToolsExecutor
from tools.artifact_read import create_read_artifact_spec
from tools import create_workspace_tool_specs
from tools.powershell_runner import PowerShellCommandRunner
from tools.workspace import WorkspaceSandbox, WorkspaceSandboxes

# 无状态内建工具通过导入注册；Workspace 工具在 composition root 中绑定。
import tools  # noqa: F401


def create_agent_application(
    model: str,
    model_context_limit: int,
    runtime_root: Path,
    workspace_roots: Mapping[str, Path],
    input_budget_ratio: float = 0.75,
    runtime_mode: RuntimeMode | None = None,
    recent_protection_tokens: int = 10_000,
    llm_client: LLMClient | None = None,
    progress_sink: RunProgressSink | None = None,
) -> AgentApplication:
    if not model or not model.strip():
        raise ValueError("model 不能为空")
    workspaces = WorkspaceSandboxes({
        name: WorkspaceSandbox(root)
        for name, root in workspace_roots.items()
    })
    runtime_root = runtime_root.resolve()
    if any(
        runtime_root.is_relative_to(workspace.root)
        for workspace in workspaces.values()
    ):
        raise ValueError("runtime_root 不能位于用户 workspace root 内")

    application_tool_registry = BUILTIN_TOOL_REGISTRY.clone()
    command_runner = PowerShellCommandRunner()
    for spec in create_workspace_tool_specs(workspaces, command_runner):
        application_tool_registry.register(spec)

    if llm_client is None:
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
    context_manager = ContextManager(result_limit.max_chars)
    summary_generator = LLMContextSummaryGenerator(model_calls, model)
    artifact_drawers = FileArtifactDrawers(runtime_root / "sessions")
    mode_configuration = (
        {"runtime_mode": runtime_mode}
        if runtime_mode is not None
        else {
            "mode_router": RuntimeModeRouter(),
            "runtime_modes": {
                RunMode.PLAIN: PlainMode(),
                RunMode.TODO: TodoMode(),
            },
        }
    )

    def create_session_run_runtime(session_id: str) -> RunRuntime:
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
        return RunRuntime(
            model_calls=model_calls,
            model=model,
            context_preparation=context_preparation,
            tools_executor=ToolsExecutor(tool_registry),
            tool_result_externalizer=ToolResultExternalizer(
                artifact_store,
                result_limit,
            ),
            progress_sink=progress_sink,
            **mode_configuration,
        )

    session_runtime = SessionRuntime(
        run_runtime_factory=create_session_run_runtime,
        delete_session_resources=artifact_drawers.delete,
    )
    goal_application = GoalApplicationService(
        session_runtime,
        InMemoryGoalStore(),
        GoalCommandBufferRegistry(),
    )
    return AgentApplication(
        session_runtime=session_runtime,
        system_prompt=DEFAULT_AGENT_PROMPT,
        goal_application=goal_application,
    )
