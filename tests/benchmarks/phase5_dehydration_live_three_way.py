from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import tiktoken

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
    build_application,
    build_registry,
    request_token_count,
)


STRATEGIES = {
    "full": "\u5168\u8131\u6c34",
    "current": "\u5f53\u524d 10K \u4fdd\u62a4\u7a97\u7b56\u7565",
    "none": "\u4e0d\u8131\u6c34",
}
TURN_ORDERS = (
    ("full", "current", "none"),
    ("current", "none", "full"),
    ("none", "full", "current"),
    ("full", "current", "none"),
    ("current", "none", "full"),
    ("none", "full", "current"),
)


def make_group(strategy: str) -> dict[str, Any]:
    store = ContentAddressedArtifactStore()
    registry = build_registry(store)
    llm = RecordingLLMClient(LLMClient())
    tools = RecordingToolsExecutor(ToolsExecutor(registry))
    application = build_application(
        llm,
        tools,
        store,
        dehydration_strategy=strategy,
    )
    session_id = application.create_session(
        f"live-{strategy}-{uuid4().hex}"
    )
    return {
        "application": application,
        "session_id": session_id,
        "llm": llm,
        "tools": tools,
        "turns": [],
        "available": True,
    }


def model_request_metrics(observation) -> dict[str, Any]:
    encoding = tiktoken.get_encoding("o200k_base")
    tool_messages = [
        message
        for message in observation.messages
        if message.get("role") == "tool"
    ]
    return {
        "offline_tokens": request_token_count(
            encoding,
            observation.messages,
            observation.tools,
        ),
        "actual_input_tokens": (
            observation.result.usage.input_tokens
            if observation.result is not None
            else None
        ),
        "message_count": len(observation.messages),
        "tool_message_count": len(tool_messages),
        "tool_chars": sum(
            len(str(message.get("content", "")))
            for message in tool_messages
        ),
        "dehydrated_stub_count": sum(
            '"externalized":true'
            in str(message.get("content", "")).replace(" ", "")
            for message in tool_messages
        ),
    }


def run_turn(group: dict[str, Any], turn: int, question: str) -> dict[str, Any]:
    if not group["available"]:
        return {
            "turn": turn,
            "question": question,
            "status": "not_run_after_terminal_failure",
            "success": False,
        }

    llm_before = len(group["llm"].observations)
    tools_before = len(group["tools"].observations)
    started = perf_counter()
    outcome = group["application"].start(
        group["session_id"],
        f"run-{turn}-{uuid4().hex}",
        question,
        max_rounds=20,
    )
    elapsed_seconds = perf_counter() - started
    llm_observations = group["llm"].observations[llm_before:]
    tool_observations = group["tools"].observations[tools_before:]
    request_checkpoints = [
        checkpoint
        for checkpoint in outcome.result.checkpoints
        if checkpoint.reason == "llm_request"
    ]
    if len(request_checkpoints) != len(llm_observations):
        raise AssertionError(
            "Checkpoint 与真实模型请求数量不一致: "
            f"{len(request_checkpoints)} != {len(llm_observations)}"
        )
    agent_requests = [
        model_request_metrics(observation)
        for checkpoint, observation in zip(
            request_checkpoints,
            llm_observations,
            strict=True,
        )
        if checkpoint.data["stage"] == "agent_round"
    ]
    successful_model_requests = [
        observation
        for observation in llm_observations
        if observation.result is not None
    ]
    successful_tools = sum(
        observation.result.get("ok") is True
        for observation in tool_observations
    )
    tool_failures = len(tool_observations) - successful_tools
    status = outcome.result.status.value
    if status in {"blocked", "failed"}:
        group["available"] = False

    return {
        "turn": turn,
        "question": question,
        "status": status,
        "success": status == "completed" and bool(outcome.result.answer.strip()),
        "elapsed_seconds": elapsed_seconds,
        "answer": outcome.result.answer,
        "final_reason": outcome.result.final_reason,
        "model_attempts": len(llm_observations),
        "successful_model_requests": len(successful_model_requests),
        "llm_retries": sum(
            checkpoint.reason == "llm_retry"
            for checkpoint in outcome.result.checkpoints
        ),
        "external_tool_calls": len(tool_observations),
        "successful_external_tools": successful_tools,
        "failed_external_tools": tool_failures,
        "agent_request_count": len(agent_requests),
        "first_context": agent_requests[0] if agent_requests else None,
        "last_context": agent_requests[-1] if agent_requests else None,
        "peak_context_tokens": max(
            (item["offline_tokens"] for item in agent_requests),
            default=0,
        ),
        "cumulative_agent_input_tokens": sum(
            item["offline_tokens"] for item in agent_requests
        ),
        "actual_agent_input_tokens": sum(
            item["actual_input_tokens"] or 0
            for item in agent_requests
        ),
    }


def summarize_group(strategy: str, group: dict[str, Any]) -> dict[str, Any]:
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
    last_context = (
        completed_turns[-1]["last_context"]
        if completed_turns
        else None
    )
    return {
        "strategy": strategy,
        "label": STRATEGIES[strategy],
        "turns": turns,
        "run_completion_rate": completed / len(QUESTIONS),
        "completed_runs": completed,
        "attempted_runs": attempted,
        "tool_success_rate": (
            tool_successes / tool_calls if tool_calls else 1.0
        ),
        "successful_external_tools": tool_successes,
        "external_tool_calls": tool_calls,
        "total_user_elapsed_seconds": sum(
            turn.get("elapsed_seconds", 0.0) for turn in turns
        ),
        "mean_user_elapsed_seconds": (
            sum(turn.get("elapsed_seconds", 0.0) for turn in turns)
            / attempted
            if attempted
            else 0.0
        ),
        "p50_user_elapsed_seconds": median(
            [
                turn["elapsed_seconds"]
                for turn in turns
                if "elapsed_seconds" in turn
            ]
        ),
        "total_offline_agent_input_tokens": sum(
            turn.get("cumulative_agent_input_tokens", 0)
            for turn in turns
        ),
        "total_actual_agent_input_tokens": sum(
            turn.get("actual_agent_input_tokens", 0)
            for turn in turns
        ),
        "final_context": last_context,
    }


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def main() -> None:
    if not WORKSPACE_ROOT.is_dir():
        raise RuntimeError(f"实验 Workspace 不存在: {WORKSPACE_ROOT}")

    groups = {
        strategy: make_group(strategy)
        for strategy in STRATEGIES
    }
    execution_order = []
    for turn, (question, order) in enumerate(
        zip(QUESTIONS, TURN_ORDERS, strict=True),
        1,
    ):
        for strategy in order:
            execution_order.append({"turn": turn, "strategy": strategy})
            result = run_turn(groups[strategy], turn, question)
            groups[strategy]["turns"].append(result)
            print(json.dumps({
                "turn": turn,
                "strategy": strategy,
                "status": result["status"],
                "elapsed_seconds": result.get("elapsed_seconds"),
                "last_context_tokens": (
                    (result.get("last_context") or {}).get("offline_tokens")
                ),
            }, ensure_ascii=False), flush=True)

    summaries = {
        strategy: summarize_group(strategy, group)
        for strategy, group in groups.items()
    }
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "experiment": "Level 1 dehydration three-way independent live runs",
        "controls": {
            "model": MODEL,
            "questions": QUESTIONS,
            "workspace_root": str(WORKSPACE_ROOT),
            "context_limit": MODEL_CONTEXT_LIMIT,
            "input_budget_ratio": INPUT_BUDGET_RATIO,
            "recent_protection_tokens": RECENT_PROTECTION_TOKENS,
            "isolation": (
                "每组独立 LLMClient、AgentApplication、Session、"
                "ContextState、ArtifactStore"
            ),
            "execution_order": execution_order,
            "timing_scope": (
                "从用户消息提交给 application.start 到 RunRuntime 返回；"
                "包含路由、模型网络、重试、工具执行、上下文处理"
            ),
            "success_definition": (
                "RunStatus=completed 且回答非空；另行报告外部工具成功率"
            ),
            "important_limitation": (
                "三组模型与工具轨迹不固定，因此结果反映真实用户体验，"
                "但差异不只来自脱水策略"
            ),
        },
        "groups": summaries,
    }
    report_path = (
        Path(__file__).resolve().parents[2]
        / "logs"
        / f"dehydration_live_three_way_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "report_path": str(report_path),
        "groups": {
            strategy: {
                key: value
                for key, value in summary.items()
                if key not in {"turns"}
            }
            for strategy, summary in summaries.items()
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
