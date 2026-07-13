from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.tools_runtime.tools_state import ToolStep, ToolsState

WRITE_TOOL_NAMES = {"apply_patch", "replace_all", "write_file"}
VERIFY_TOOL_NAMES = {"get_changes"}


@dataclass
class Checkpoint:
    kind: str
    reason: str
    message: str
    data: dict[str, Any]


def checkpoint_to_record(checkpoint: Checkpoint) -> dict[str, Any]:
    return asdict(checkpoint)


def successful_writes(tools_state: ToolsState) -> list[ToolStep]:
    return [
        step
        for step in tools_state.steps
        if step.name in WRITE_TOOL_NAMES and step.ok is True
    ]


def verified_after_last_write(tools_state: ToolsState) -> bool:
    last_write_index = None
    for index, step in enumerate(tools_state.steps):
        if step.name in WRITE_TOOL_NAMES and step.ok is True:
            last_write_index = index

    if last_write_index is None:
        return True

    return any(
        step.name in VERIFY_TOOL_NAMES and step.ok is True
        for step in tools_state.steps[last_write_index + 1:]
    )


def needs_verification(tools_state: ToolsState) -> bool:
    return bool(successful_writes(tools_state)) and not verified_after_last_write(tools_state)


def verification_status(tools_state: ToolsState) -> dict[str, Any]:
    return {
        "successful_writes": len(successful_writes(tools_state)),
        "verified_after_last_write": verified_after_last_write(tools_state),
        "needs_verification": needs_verification(tools_state),
    }


def run_started_checkpoint(max_rounds: int) -> Checkpoint:
    return Checkpoint(
        kind="run",
        reason="run_started",
        message="运行开始。",
        data={
            "max_rounds": max_rounds,
        },
    )


def run_completed_checkpoint(answer: str, extra_data: dict[str, Any] | None = None) -> Checkpoint:
    data: dict[str, Any] = {
        "answer_length": len(answer),
    }
    if extra_data:
        data.update(extra_data)
    return Checkpoint(
        kind="run",
        reason="run_completed",
        message="运行完成。",
        data=data,
    )


def tool_batch_completed_checkpoint(
    round_index: int,
    tools_state: ToolsState,
    batch_size: int,
    extra_data: dict[str, Any] | None = None,
) -> Checkpoint:
    data = {
        "round_index": round_index,
        "batch_size": batch_size,
        "tools": tools_state.status(),
        "verification": verification_status(tools_state),
    }
    if extra_data:
        data.update(extra_data)
    return Checkpoint(
        kind="tool_batch",
        reason="tool_batch_completed",
        message=f"第 {round_index} 轮工具调用已完成。",
        data=data,
    )


def message_chain_invalid_checkpoint(validation: dict[str, Any]) -> Checkpoint:
    return Checkpoint(
        kind="terminate",
        reason="message_chain_invalid",
        message="运行已停止：messages 中的工具调用链路不合法。",
        data={
            "validation": validation,
        },
    )


def context_length_exceeded_checkpoint(*, round_index: int, error: str) -> Checkpoint:
    return Checkpoint(
        kind="terminate",
        reason="context_length_exceeded",
        message="运行已停止：上下文超过模型限制，当前阶段暂不做自动裁剪。",
        data={
            "round_index": round_index,
            "error": error,
            "hint": "上下文压缩会在后续 ContextCompactor 阶段实现。",
        },
    )


def budget_stop_checkpoint(max_rounds: int, tools_state: ToolsState) -> Checkpoint:
    tools_status = tools_state.status()
    verify_status = verification_status(tools_state)
    message = f"运行已停止：达到最大轮次 max_rounds={max_rounds}。"
    return Checkpoint(
        kind="terminate",
        reason="max_rounds_exceeded",
        message=message,
        data={
            "max_rounds": max_rounds,
            "tools": tools_status,
            "verification": verify_status,
        },
    )


def format_checkpoint(checkpoint: Checkpoint) -> str:
    if checkpoint.reason == "message_chain_invalid":
        validation = checkpoint.data["validation"]
        lines = [checkpoint.message]
        for error in validation["errors"]:
            lines.append(f"- {error}")
        return "\n".join(lines)

    if checkpoint.reason == "llm_error":
        return "\n".join([
            checkpoint.message,
            f"轮次：{checkpoint.data['round_index']}，重试次数：{checkpoint.data['attempts']}。",
            f"错误：{checkpoint.data['error']}",
        ])

    if checkpoint.reason == "context_length_exceeded":
        return "\n".join([
            checkpoint.message,
            f"轮次：{checkpoint.data['round_index']}。",
            f"错误：{checkpoint.data['error']}",
            f"提示：{checkpoint.data['hint']}",
        ])

    tools = checkpoint.data["tools"]
    verification = checkpoint.data["verification"]
    lines = [
        checkpoint.message,
        f"工具链状态：total={tools['total']}, pending={tools['pending']}, failed={tools['failed']}, balanced={tools['balanced']}。",
    ]
    if verification["needs_verification"]:
        lines.append("注意：本次运行中已有写入类工具成功执行，但还没有在最后一次写入后完成 get_changes 验证。")
    lines.append("这不是任务成功完成，而是预算耗尽后的安全停止。")
    return "\n".join(lines)


def llm_retry_checkpoint(
    *,
    round_index: int,
    attempt: int,
    max_attempts: int,
    error: str,
) -> Checkpoint:
    return Checkpoint(
        kind="llm",
        reason="llm_retry",
        message=f"第 {round_index} 轮 LLM 调用失败，准备重试 ({attempt}/{max_attempts})。",
        data={
            "round_index": round_index,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error": error,
        },
    )


def llm_error_checkpoint(*, round_index: int, attempts: int, error: str) -> Checkpoint:
    return Checkpoint(
        kind="terminate",
        reason="llm_error",
        message="运行已停止：LLM 调用失败，重试已耗尽。",
        data={
            "round_index": round_index,
            "attempts": attempts,
            "error": error,
        },
    )
