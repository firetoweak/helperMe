from __future__ import annotations

from pathlib import Path

from core.agent_application import AgentApplication
from core.model_call.client import LLMClient
from core.prompt import DEFAULT_AGENT_PROMPT
from core.runtime_modes import RuntimeMode
from core.todos import TodoMode
from core.session_runner import SessionRuntime
from core.tools_runtime.run_runtime import RunRuntime
from core.context import (
    ContextBudget,
    ContextManager,
    ContextPreparationService,
    MicroCompactionConfig,
    MicroCompactionPolicy,
    ModelBudgetConfig,
    TiktokenTokenEstimator,
)
from core.model_call.service import ModelCallService
from core.context_summary import LLMContextSummaryGenerator
from core.runtime_artifacts import (
    FileArtifactStore,
    ToolResultExternalizer,
    ToolResultLimit,
)
from core.tool_registry import BUILTIN_TOOL_REGISTRY
from core.tools_runtime.tools_executor import ToolsExecutor
from tools.artifact_read import create_read_artifact_spec
from tools.workspace import WORKSPACE

# 工具目前通过导入副作用注册；composition root 是唯一组装入口。
import tools  # noqa: F401


def create_agent_application(
    model: str,
    model_context_limit: int,
    runtime_root: Path,
    input_budget_ratio: float = 0.75,
    runtime_mode: RuntimeMode | None = None,
    recent_protection_tokens: int = 10_000,
) -> AgentApplication:
    if not model or not model.strip():
        raise ValueError("model 不能为空")
    runtime_root = runtime_root.resolve()
    if runtime_root.is_relative_to(WORKSPACE.resolve()):
        raise ValueError("runtime_root 不能位于用户 workspace 内")

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
    artifact_store = FileArtifactStore(runtime_root / "artifacts")
    tool_registry = BUILTIN_TOOL_REGISTRY.clone()
    tool_registry.register(create_read_artifact_spec(artifact_store))
    context_manager = ContextManager(result_limit.max_chars)
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
        summary_generator=LLMContextSummaryGenerator(model_calls, model),
    )
    run_runtime = RunRuntime(
        model_calls=model_calls,
        model=model,
        runtime_mode=(
            runtime_mode if runtime_mode is not None else TodoMode()
        ),
        context_preparation=context_preparation,
        tools_executor=ToolsExecutor(tool_registry),
        tool_result_externalizer=ToolResultExternalizer(
            artifact_store,
            result_limit,
        ),
    )
    session_runtime = SessionRuntime(run_runtime)
    return AgentApplication(
        session_runtime=session_runtime,
        system_prompt=DEFAULT_AGENT_PROMPT,
    )
