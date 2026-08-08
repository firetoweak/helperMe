from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core.session import SessionRunOutcome
from core.tools_runtime.tools_checkpoint import checkpoint_to_record


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_default_run_log_path() -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"session_{stamp}.log"


def build_run_trace(
    *,
    started_at: str,
    model: str,
    question: str,
    outcome: SessionRunOutcome,
) -> dict[str, Any]:
    result = outcome.result
    run_started = next(
        checkpoint
        for checkpoint in result.checkpoints
        if checkpoint.reason == "run_started"
    )
    model_requests = [
        checkpoint.data
        for checkpoint in result.checkpoints
        if checkpoint.reason == "llm_request"
    ]
    return {
        "type": "agent_run",
        "started_at": started_at,
        "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "system_prompt": run_started.data["system_prompt"],
        "model_requests": model_requests,
        "run_id": outcome.record.run_id,
        "question": question,
        "answer": result.answer,
        "status": result.status.value,
        "final_reason": result.final_reason,
        "checkpoints": [
            checkpoint_to_record(checkpoint)
            for checkpoint in result.checkpoints
            if checkpoint.reason != "llm_request"
        ],
    }


def format_run_log(trace: dict[str, Any]) -> str:
    lines = [
        "",
        "=" * 76,
        f"Agent Run | {trace['started_at']} -> {trace['ended_at']}",
        f"Model: {trace['model']} | Run: {trace['run_id']} | Status: {trace['status']}",
    ]
    if trace.get("final_reason"):
        lines.append(f"Final reason: {trace['final_reason']}")
    lines.extend(
        [
            "-" * 76,
            "Question:",
            str(trace["question"]),
            "",
            "System Prompt:",
            str(trace["system_prompt"]),
            "",
            "Model Requests (Runtime Prompts + Full Messages):",
            json.dumps(trace["model_requests"], ensure_ascii=False, indent=2),
            "",
            "Answer:",
            str(trace["answer"]),
            "",
            "Checkpoints:",
            json.dumps(trace["checkpoints"], ensure_ascii=False, indent=2),
            "=" * 76,
            "",
        ]
    )
    return "\n".join(lines)


def write_run_log(trace: dict[str, Any], path: Path) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(format_run_log(trace))
