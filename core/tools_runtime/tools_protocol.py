from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from core.tools_runtime.tools_state import ToolStep


@dataclass
class MessageValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    pending_tool_call_ids: list[str] = field(default_factory=list)
    orphan_tool_result_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "pending_tool_call_ids": self.pending_tool_call_ids,
            "orphan_tool_result_ids": self.orphan_tool_result_ids,
        }


def validate_tool_message_chain(
    messages: list[dict[str, Any]],
) -> MessageValidationResult:
    """校验 assistant tool_calls 与 tool results 的配对和顺序。"""
    errors: list[str] = []
    orphan_tool_result_ids: list[str] = []
    pending: list[str] = []

    for index, message in enumerate(messages):
        role = message.get("role")
        tool_calls = message.get("tool_calls") or []

        if pending:
            if role != "tool":
                errors.append(
                    f"message[{index}] role={role} appeared before pending tool results: {pending}"
                )
            else:
                tool_call_id = message.get("tool_call_id")
                if tool_call_id not in pending:
                    orphan_tool_result_ids.append(str(tool_call_id))
                    errors.append(f"message[{index}] orphan tool result: {tool_call_id}")
                elif tool_call_id != pending[0]:
                    errors.append(
                        f"message[{index}] tool result order mismatch: "
                        f"expected {pending[0]}, got {tool_call_id}"
                    )
                    pending.remove(tool_call_id)
                else:
                    pending.pop(0)
            continue

        if role == "assistant" and tool_calls:
            pending = [call["id"] for call in tool_calls]
            continue

        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            orphan_tool_result_ids.append(str(tool_call_id))
            errors.append(
                f"message[{index}] tool result without pending tool call: {tool_call_id}"
            )

    if pending:
        errors.append(f"dangling tool calls: {pending}")

    return MessageValidationResult(
        ok=not errors,
        errors=errors,
        pending_tool_call_ids=pending,
        orphan_tool_result_ids=orphan_tool_result_ids,
    )


def build_tool_messages(
    steps: Iterable[ToolStep],
    encoder: Callable[[dict[str, Any]], str],
) -> list[dict[str, str]]:
    """把已完成的 ToolStep 转换为 OpenAI tool messages。"""
    return [
        {
            "tool_call_id": step.call_id,
            "content": encoder(step.result),
        }
        for step in steps
        if step.result is not None
    ]
