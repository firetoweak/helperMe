from __future__ import annotations

from core.agent_application import AgentApplication
from core.llm_client import LLMClient
from core.prompt import DEFAULT_AGENT_PROMPT
from core.runtime_modes import RuntimeMode
from core.session_runner import SessionRuntime
from core.tools_runtime.run_runtime import RunRuntime

# 工具目前通过导入副作用注册；composition root 是唯一组装入口。
import tools  # noqa: F401


def create_agent_application(
    model: str,
    runtime_mode: RuntimeMode | None = None,
) -> AgentApplication:
    if not model or not model.strip():
        raise ValueError("model 不能为空")

    llm_client = LLMClient()
    run_runtime = RunRuntime(
        llm_client=llm_client,
        model=model,
        runtime_mode=runtime_mode,
    )
    session_runtime = SessionRuntime(run_runtime)
    return AgentApplication(
        session_runtime=session_runtime,
        system_prompt=DEFAULT_AGENT_PROMPT,
    )
