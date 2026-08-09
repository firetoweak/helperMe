from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from time import perf_counter

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
    ReplayLLMClient,
    ReplayToolsExecutor,
    ToolsExecutor,
    build_application,
    build_registry,
    run_group,
)


STRATEGIES = (
    ("full", "\u5168\u8131\u6c34"),
    ("current", "\u5f53\u524d 10K \u4fdd\u62a4\u7a97\u7b56\u7565"),
    ("none", "\u4e0d\u8131\u6c34"),
)


def run_replay_group(
    strategy: str,
    recorded_llm,
    recorded_tools,
) -> dict:
    store = ContentAddressedArtifactStore()
    registry = build_registry(store)
    replay_llm = ReplayLLMClient(recorded_llm)
    replay_tools = ReplayToolsExecutor(registry, recorded_tools)
    application = build_application(
        replay_llm,
        replay_tools,
        store,
        dehydration_strategy=strategy,
    )

    started = perf_counter()
    turns, _ = run_group(application, replay_llm.observations)
    elapsed_seconds = perf_counter() - started

    if replay_llm.index != len(recorded_llm):
        raise AssertionError(f"{strategy} 未完整消费模型轨迹")
    if replay_tools.index != len(recorded_tools):
        raise AssertionError(f"{strategy} 未完整消费工具轨迹")

    return {
        "strategy": strategy,
        "execution_time_seconds": elapsed_seconds,
        "turns": turns,
        "total_agent_input_tokens": sum(
            turn["cumulative_tokens"] for turn in turns
        ),
        "final_context_tokens": turns[-1]["last"]["tokens"],
        "peak_context_tokens": max(
            turn["peak_tokens"] for turn in turns
        ),
        "final_tool_chars": turns[-1]["last"]["tool_chars"],
        "final_dehydrated_stub_count": (
            turns[-1]["last"]["dehydrated_stub_count"]
        ),
    }


def comparison(groups: dict[str, dict]) -> dict:
    none = groups["none"]
    rows = []
    for strategy, label in STRATEGIES:
        group = groups[strategy]
        rows.append({
            "strategy": strategy,
            "label": label,
            "execution_time_seconds": group["execution_time_seconds"],
            "total_agent_input_tokens": group["total_agent_input_tokens"],
            "total_saved_vs_none": (
                none["total_agent_input_tokens"]
                - group["total_agent_input_tokens"]
            ),
            "total_saved_ratio_vs_none": (
                (
                    none["total_agent_input_tokens"]
                    - group["total_agent_input_tokens"]
                )
                / none["total_agent_input_tokens"]
            ),
            "final_context_tokens": group["final_context_tokens"],
            "final_saved_vs_none": (
                none["final_context_tokens"]
                - group["final_context_tokens"]
            ),
            "peak_context_tokens": group["peak_context_tokens"],
            "final_tool_chars": group["final_tool_chars"],
            "final_dehydrated_stub_count": (
                group["final_dehydrated_stub_count"]
            ),
        })
    return {"groups": rows}


def main() -> None:
    if not WORKSPACE_ROOT.is_dir():
        raise RuntimeError(f"实验 Workspace 不存在: {WORKSPACE_ROOT}")

    seed_store = ContentAddressedArtifactStore()
    seed_registry = build_registry(seed_store)
    recording_llm = RecordingLLMClient(LLMClient())
    recording_tools = RecordingToolsExecutor(ToolsExecutor(seed_registry))
    seed_application = build_application(
        recording_llm,
        recording_tools,
        seed_store,
        dehydration_strategy="current",
    )
    seed_started = perf_counter()
    seed_turns, _ = run_group(
        seed_application,
        recording_llm.observations,
    )
    seed_elapsed_seconds = perf_counter() - seed_started

    groups = {}
    for strategy, _ in STRATEGIES:
        groups[strategy] = run_replay_group(
            strategy,
            recording_llm.observations,
            recording_tools.observations,
        )

    statuses = {
        turn["status"]
        for group in groups.values()
        for turn in group["turns"]
    }
    if statuses != {"completed"}:
        raise AssertionError(f"存在未完成实验轮次: {statuses}")
    reference_answers = [turn["answer"] for turn in seed_turns]
    for strategy, group in groups.items():
        if [turn["answer"] for turn in group["turns"]] != reference_answers:
            raise AssertionError(f"{strategy} 回答轨迹与种子轨迹不一致")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "experiment": "Level 1 dehydration three-way controlled replay",
        "controls": {
            "model": MODEL,
            "questions": QUESTIONS,
            "workspace_root": str(WORKSPACE_ROOT),
            "context_limit": MODEL_CONTEXT_LIMIT,
            "input_budget_ratio": INPUT_BUDGET_RATIO,
            "recent_protection_tokens": RECENT_PROTECTION_TOKENS,
            "seed_execution_time_seconds": seed_elapsed_seconds,
            "seed_note": "只用于录制，不进入三组执行时间比较",
            "model_responses": "三组重放完全相同的种子响应与失败重试",
            "tool_results": "三组重放完全相同的工具结果",
            "tokenizer": "o200k_base over canonical {messages, tools} JSON",
            "execution_time_scope": (
                "本地 RunRuntime、上下文投影、脱水、协议编排与内存重放；"
                "不含真实模型和工具 I/O 延迟"
            ),
            "only_difference": {
                "full": "recent_start_index 固定为 records 末尾",
                "current": "当前 MicroCompactionPolicy，10K 保护窗",
                "none": "eligible tool message ids 固定为空",
            },
        },
        "groups": groups,
        "comparison": comparison(groups),
    }
    report_path = (
        Path(__file__).resolve().parents[2]
        / "logs"
        / f"dehydration_three_way_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "report_path": str(report_path),
        "seed_execution_time_seconds": seed_elapsed_seconds,
        "comparison": report["comparison"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
