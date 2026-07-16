from __future__ import annotations

from core.agent_application import AgentApplication
from core.model_call.client import LLMClient
from core.prompt import DEFAULT_AGENT_PROMPT
from core.runtime_modes import PlainMode, RuntimeMode
from core.session_runner import SessionRuntime
from core.tools_runtime.run_runtime import RunRuntime
from core.context import (
    ContextBudget,
    ContextManager,
    ModelBudgetConfig,
    TiktokenTokenEstimator,
)
from core.model_call.service import ModelCallService

# 工具目前通过导入副作用注册；composition root 是唯一组装入口。
import tools  # noqa: F401


def create_agent_application(
    model: str,
    model_context_limit: int,
    input_budget_ratio: float = 0.75,
    runtime_mode: RuntimeMode | None = None,
) -> AgentApplication:
    if not model or not model.strip():
        raise ValueError("model 不能为空")

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
    run_runtime = RunRuntime(
        model_calls=model_calls,
        model=model,
        runtime_mode=runtime_mode if runtime_mode is not None else PlainMode(),
        context_manager=ContextManager(),
    )
    session_runtime = SessionRuntime(run_runtime)
    return AgentApplication(
        session_runtime=session_runtime,
        system_prompt=DEFAULT_AGENT_PROMPT,
    )
