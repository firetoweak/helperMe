from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from tests.benchmarks.phase5_dehydration_ab import (
    INPUT_BUDGET_RATIO,
    MODEL,
    MODEL_CONTEXT_LIMIT,
    QUESTIONS,
    RECENT_PROTECTION_TOKENS,
    WORKSPACE_ROOT,
)
from tests.benchmarks.phase5_dehydration_live_three_way import (
    make_group,
    run_turn,
    summarize_group,
)


def main() -> None:
    if not WORKSPACE_ROOT.is_dir():
        raise RuntimeError(f"实验 Workspace 不存在: {WORKSPACE_ROOT}")

    group = make_group("full")
    for turn, question in enumerate(QUESTIONS, 1):
        result = run_turn(group, turn, question)
        group["turns"].append(result)
        print(json.dumps({
            "turn": turn,
            "status": result["status"],
            "elapsed_seconds": result.get("elapsed_seconds"),
            "final_reason": result.get("final_reason"),
            "agent_request_count": result.get("agent_request_count"),
            "external_tool_calls": result.get("external_tool_calls"),
            "last_context": result.get("last_context"),
        }, ensure_ascii=False), flush=True)

    summary = summarize_group("full", group)
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "experiment": "Full dehydration independent live rerun",
        "controls": {
            "model": MODEL,
            "questions": QUESTIONS,
            "workspace_root": str(WORKSPACE_ROOT),
            "context_limit": MODEL_CONTEXT_LIMIT,
            "input_budget_ratio": INPUT_BUDGET_RATIO,
            "recent_protection_tokens": RECENT_PROTECTION_TOKENS,
            "max_steps": 20,
            "timing_scope": (
                "从 application.start 到 TurnRuntime 返回；包含路由、模型网络、"
                "重试、工具执行与上下文处理"
            ),
            "important_limitation": (
                "模型与工具轨迹未固定；用于检查全脱水失败是否稳定复现，"
                "不能把单次差异严格归因于脱水"
            ),
        },
        "full_dehydration": summary,
    }
    report_path = (
        Path(__file__).resolve().parents[2]
        / "logs"
        / f"dehydration_live_full_replay_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "report_path": str(report_path),
        "summary": {
            key: value
            for key, value in summary.items()
            if key != "turns"
        },
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
