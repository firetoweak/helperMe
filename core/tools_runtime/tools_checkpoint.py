from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.context import BudgetAssessment
from core.model_call.types import LLMUsage
from core.tools_runtime.stop_guard import verification_status
from core.tools_runtime.tools_state import ToolsState


@dataclass
class Checkpoint:
    kind: str
    reason: str
    message: str
    data: dict[str, Any]


def checkpoint_to_record(checkpoint: Checkpoint) -> dict[str, Any]:
    return asdict(checkpoint)


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
        "tools": tools_state.summary(),
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
        kind="run",
        reason="message_chain_invalid",
        message="运行已停止：messages 中的工具调用链路不合法。",
        data={
            "validation": validation,
        },
    )


def context_length_exceeded_checkpoint(
    *,
    stage: str,
    error: str,
    round_index: int | None = None,
) -> Checkpoint:
    return Checkpoint(
        kind="run",
        reason="context_length_exceeded",
        message="运行已停止：上下文超过模型限制，当前阶段暂不做自动裁剪。",
        data={
            "stage": stage,
            "round_index": round_index,
            "error": error,
            "hint": "上下文压缩会在后续 ContextCompactor 阶段实现。",
        },
    )


def context_budget_exceeded_checkpoint(
    *,
    stage: str,
    assessment: BudgetAssessment,
    round_index: int | None = None,
) -> Checkpoint:
    return Checkpoint(
        kind="run",
        reason="context_budget_exceeded",
        message="运行已停止：当前模型输入超过项目上下文预算。",
        data={
            "stage": stage,
            "round_index": round_index,
            "estimated_input_tokens": assessment.estimated_input_tokens,
            "input_budget_tokens": assessment.input_budget_tokens,
            "overflow_tokens": assessment.overflow_tokens,
            "hint": "上下文压缩会在后续 Safe Compression 阶段实现。",
        },
    )


def invalid_llm_response_checkpoint(
    *,
    round_index: int,
    reason: str,
    error: str,
) -> Checkpoint:
    return Checkpoint(
        kind="run",
        reason=reason,
        message="运行已停止：LLM 返回了非法响应。",
        data={
            "round_index": round_index,
            "error": error,
        },
    )


def budget_stop_checkpoint(max_rounds: int, tools_state: ToolsState) -> Checkpoint:
    tools_status = tools_state.summary()
    verify_status = verification_status(tools_state)
    message = f"运行已停止：达到最大轮次 max_rounds={max_rounds}。"
    return Checkpoint(
        kind="run",
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
        lines = [
            checkpoint.message,
            f"阶段：{checkpoint.data['stage']}。",
        ]
        if checkpoint.data["round_index"] is not None:
            lines.append(f"轮次：{checkpoint.data['round_index']}。")
        lines.extend([
            f"错误：{checkpoint.data['error']}",
            f"提示：{checkpoint.data['hint']}",
        ])
        return "\n".join(lines)

    if checkpoint.reason == "context_budget_exceeded":
        lines = [
            checkpoint.message,
            f"阶段：{checkpoint.data['stage']}。",
        ]
        if checkpoint.data["round_index"] is not None:
            lines.append(f"轮次：{checkpoint.data['round_index']}。")
        lines.extend([
            (
                "估算输入："
                f"{checkpoint.data['estimated_input_tokens']}，"
                "项目预算："
                f"{checkpoint.data['input_budget_tokens']}，"
                "超出："
                f"{checkpoint.data['overflow_tokens']}。"
            ),
            f"提示：{checkpoint.data['hint']}",
        ])
        return "\n".join(lines)

    if checkpoint.reason in {"empty_model_response", "invalid_llm_response"}:
        return "\n".join([
            checkpoint.message,
            f"轮次：{checkpoint.data['round_index']}。",
            f"错误：{checkpoint.data['error']}",
        ])

    if checkpoint.reason == "verification_required":
        return checkpoint.message

    tools = checkpoint.data["tools"]
    verification = checkpoint.data["verification"]
    lines = [
        checkpoint.message,
        f"工具链状态：total={tools['total']}, pending={tools['pending']}, failed={tools['failed']}。",
    ]
    if verification["needs_verification"]:
        lines.append("注意：本次运行中已有写入类工具成功执行，但还没有在最后一次写入后完成 get_changes 验证。")
    lines.append("这不是任务成功完成，而是预算耗尽后的安全停止。")
    return "\n".join(lines)


def verification_required_checkpoint() -> Checkpoint:
    return Checkpoint(
        kind="runtime_feedback",
        reason="verification_required",
        message=(
            "检测到写入类工具已经成功执行，但尚未调用 get_changes 验证。"
            "在最终回答或中断前，必须先完成验证。"
        ),
        data={},
    )


def run_interrupted_checkpoint(reason: str | None = None) -> Checkpoint:
    return Checkpoint(
        kind="run",
        reason="run_interrupted",
        message="运行已在安全点中断。",
        data={"request_reason": reason},
    )


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


def llm_usage_checkpoint(
    *,
    stage: str,
    usage: LLMUsage,
    round_index: int | None = None,
) -> Checkpoint:
    return Checkpoint(
        kind="llm",
        reason="llm_usage",
        message="已记录模型真实 token 消耗。",
        data={
            "stage": stage,
            "round_index": round_index,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
    )


def llm_error_checkpoint(*, round_index: int, attempts: int, error: str) -> Checkpoint:
    return Checkpoint(
        kind="run",
        reason="llm_error",
        message="运行已停止：LLM 调用失败，重试已耗尽。",
        data={
            "round_index": round_index,
            "attempts": attempts,
            "error": error,
        },
    )
