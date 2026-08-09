from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.agent_application import AgentApplication
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
from core.prompt import DEFAULT_AGENT_PROMPT
from core.runtime_artifacts import ToolResultExternalizer, ToolResultLimit
from core.runtime_modes import PlainMode, RunMode, RuntimeModeRouter
from core.session import SessionRuntime
from core.todos import TodoMode
from core.tools_runtime.run_runtime import RunRuntime
from tests.benchmarks.phase5_dehydration_ab import (
    ContentAddressedArtifactStore,
    INPUT_BUDGET_RATIO,
    LLMClient,
    MODEL,
    MODEL_CONTEXT_LIMIT,
    QUESTIONS,
    RECENT_PROTECTION_TOKENS,
    WORKSPACE_ROOT,
    RecordingLLMClient,
    RecordingToolsExecutor,
    ToolsExecutor,
    build_registry,
)
from tests.benchmarks.phase5_dehydration_live_three_way import (
    median,
    run_turn,
)


PREVIOUS_REPORT = Path(
    r"D:\work\helpMe\helperMe\logs\dehydration_live_three_way_2026-08-09_15-27-47.json"
)


class RunGranularityPolicy(MicroCompactionPolicy):
    """每个 Run 只允许第一次 Context Preparation 发现新的脱水对象。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._allow_new_dehydration = False

    def begin_run(self) -> None:
        self._allow_new_dehydration = True

    def propose(self, *args, **kwargs):
        try:
            return super().propose(*args, **kwargs)
        finally:
            self._allow_new_dehydration = False

    def _eligible_tool_message_ids(
        self,
        records,
        *,
        max_index_exclusive: int,
    ) -> list[str]:
        if not self._allow_new_dehydration:
            return []
        return super()._eligible_tool_message_ids(
            records,
            max_index_exclusive=max_index_exclusive,
        )


class RunGranularityContextPreparationService(ContextPreparationService):
    """用本 Run 的固定 Level 2 边界识别 Run 切换。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_run_boundary: object = object()

    def prepare(self, *args, level2_boundary_message_id=None, **kwargs):
        if level2_boundary_message_id != self._last_run_boundary:
            self.micro_compaction_policy.begin_run()
            self._last_run_boundary = level2_boundary_message_id
        return super().prepare(
            *args,
            level2_boundary_message_id=level2_boundary_message_id,
            **kwargs,
        )


def build_run_granularity_application(
    llm_client,
    tools_executor,
    artifact_store,
) -> AgentApplication:
    context_budget = ContextBudget(
        estimator=TiktokenTokenEstimator(),
        config=ModelBudgetConfig(
            context_limit=MODEL_CONTEXT_LIMIT,
            input_ratio=INPUT_BUDGET_RATIO,
        ),
    )
    model_calls = ModelCallService(llm_client, context_budget)
    context_manager = ContextManager(ToolResultLimit().max_chars)
    policy = RunGranularityPolicy(
        context_manager=context_manager,
        context_budget=context_budget,
        config=MicroCompactionConfig(
            recent_protection_tokens=RECENT_PROTECTION_TOKENS,
        ),
        artifact_store=artifact_store,
    )
    context_preparation = RunGranularityContextPreparationService(
        context_manager=context_manager,
        micro_compaction_policy=policy,
        context_budget=context_budget,
        summary_generator=LLMContextSummaryGenerator(model_calls, MODEL),
    )
    run_runtime = RunRuntime(
        model_calls=model_calls,
        model=MODEL,
        mode_router=RuntimeModeRouter(),
        runtime_modes={
            RunMode.PLAIN: PlainMode(),
            RunMode.TODO: TodoMode(),
        },
        context_preparation=context_preparation,
        tools_executor=tools_executor,
        tool_result_externalizer=ToolResultExternalizer(
            artifact_store,
            ToolResultLimit(),
        ),
    )
    return AgentApplication(
        session_runtime=SessionRuntime(run_runtime=run_runtime),
        system_prompt=DEFAULT_AGENT_PROMPT,
    )


def make_group() -> dict[str, Any]:
    store = ContentAddressedArtifactStore()
    registry = build_registry(store)
    llm = RecordingLLMClient(LLMClient())
    tools = RecordingToolsExecutor(ToolsExecutor(registry))
    application = build_run_granularity_application(llm, tools, store)
    session_id = application.create_session(
        f"live-run-granularity-{uuid4().hex}"
    )
    return {
        "application": application,
        "session_id": session_id,
        "llm": llm,
        "tools": tools,
        "turns": [],
        "available": True,
    }


def summarize(group: dict[str, Any]) -> dict[str, Any]:
    turns = group["turns"]
    completed = sum(turn["status"] == "completed" for turn in turns)
    attempted = sum(
        turn["status"] != "not_run_after_terminal_failure"
        for turn in turns
    )
    tool_calls = sum(turn.get("external_tool_calls", 0) for turn in turns)
    tool_successes = sum(
        turn.get("successful_external_tools", 0) for turn in turns
    )
    completed_turns = [
        turn for turn in turns if turn["status"] == "completed"
    ]
    elapsed = [
        turn["elapsed_seconds"]
        for turn in turns
        if "elapsed_seconds" in turn
    ]
    return {
        "strategy": "run_granularity_10k",
        "label": "Run 维度 10K 保护窗策略",
        "turns": turns,
        "run_completion_rate": completed / len(QUESTIONS),
        "completed_runs": completed,
        "attempted_runs": attempted,
        "tool_success_rate": tool_successes / tool_calls if tool_calls else 1.0,
        "successful_external_tools": tool_successes,
        "external_tool_calls": tool_calls,
        "total_user_elapsed_seconds": sum(elapsed),
        "mean_user_elapsed_seconds": sum(elapsed) / attempted if attempted else 0.0,
        "p50_user_elapsed_seconds": median(elapsed),
        "total_offline_agent_input_tokens": sum(
            turn.get("cumulative_agent_input_tokens", 0) for turn in turns
        ),
        "total_actual_agent_input_tokens": sum(
            turn.get("actual_agent_input_tokens", 0) for turn in turns
        ),
        "final_context": (
            completed_turns[-1]["last_context"] if completed_turns else None
        ),
    }


def previous_baselines() -> dict[str, Any]:
    report = json.loads(PREVIOUS_REPORT.read_text(encoding="utf-8"))
    return {
        key: report["groups"][key]
        for key in ("current", "none")
    }


def main() -> None:
    if not WORKSPACE_ROOT.is_dir():
        raise RuntimeError(f"实验 Workspace 不存在: {WORKSPACE_ROOT}")
    if not PREVIOUS_REPORT.is_file():
        raise RuntimeError(f"旧实验报告不存在: {PREVIOUS_REPORT}")

    group = make_group()
    for turn, question in enumerate(QUESTIONS, 1):
        result = run_turn(group, turn, question)
        group["turns"].append(result)
        print(json.dumps({
            "turn": turn,
            "status": result["status"],
            "elapsed_seconds": result.get("elapsed_seconds"),
            "external_tool_calls": result.get("external_tool_calls"),
            "last_context": result.get("last_context"),
        }, ensure_ascii=False), flush=True)

    summary = summarize(group)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "experiment": "Level 1 dehydration at Run granularity, independent live runs",
        "controls": {
            "model": MODEL,
            "questions": QUESTIONS,
            "workspace_root": str(WORKSPACE_ROOT),
            "context_limit": MODEL_CONTEXT_LIMIT,
            "input_budget_ratio": INPUT_BUDGET_RATIO,
            "recent_protection_tokens": RECENT_PROTECTION_TOKENS,
            "trigger_granularity": (
                "每个 application.start 仅第一次 Context Preparation "
                "重新计算脱水集合；同 Run 后续 round 只复用既有 ContextState"
            ),
            "timing_scope": (
                "从用户消息提交给 application.start 到 RunRuntime 返回；"
                "包含路由、模型网络、重试、工具执行、上下文处理"
            ),
            "important_limitation": (
                "本组与旧实验没有固定模型及工具轨迹，比较反映真实用户体验，"
                "但差异不能严格归因于脱水粒度"
            ),
            "previous_report": str(PREVIOUS_REPORT),
        },
        "run_granularity": summary,
        "previous_baselines": previous_baselines(),
    }
    report_path = (
        Path(__file__).resolve().parents[2]
        / "logs"
        / f"dehydration_live_run_granularity_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "report_path": str(report_path),
        "run_granularity": {
            key: value
            for key, value in summary.items()
            if key != "turns"
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
