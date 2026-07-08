from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


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


@dataclass
class ToolStep:
    call_id: str
    name: str
    arguments: str
    result: dict[str, Any] | None = None
    ok: bool | None = None
    code: str | None = None
    error: str | None = None


class ToolsState:
    def __init__(self):
        self.steps: list[ToolStep] = []

    def add_call(self, call_id: str, name: str, arguments: str) -> ToolStep:
        if self.find_step(call_id) is not None:
            raise ValueError(f"duplicate tool call: {call_id}")
        step = ToolStep(
            call_id=call_id,
            name=name,
            arguments=arguments,
        )
        self.steps.append(step)
        return step

    def add_calls(self, calls: Iterable[Any]) -> list[ToolStep]:
        return [
            self.add_call(call.id, call.name, call.arguments)
            for call in calls
        ]

    def add_result(self, call_id: str, result: dict[str, Any]) -> None:
        step = self.get_step(call_id)
        step.result = result
        step.ok = result.get("ok")
        step.code = result.get("code")
        step.error = result.get("error")

    def find_step(self, call_id: str) -> ToolStep | None:
        for step in self.steps:
            if step.call_id == call_id:
                return step
        return None

    def get_step(self, call_id: str) -> ToolStep:
        step = self.find_step(call_id)
        if step is not None:
            return step
        raise ValueError(f"tool call not found: {call_id}")

    def pending_calls(self) -> list[ToolStep]:
        """工具调用总步长"""
        return [step for step in self.steps if step.result is None]

    def is_balanced(self) -> bool:
        return not self.pending_calls()

    def has_pending(self) -> bool:
        return bool(self.pending_calls())

    def status(self) -> dict[str, Any]:
        pending = self.pending_calls()
        return {
            "total": len(self.steps),
            "pending": len(pending),
            "balanced": len(pending) == 0,
            "failed": len([step for step in self.steps if step.ok is False]),
        }

    def validate_messages(self, messages: list[dict[str, Any]]) -> MessageValidationResult:
        """
        校验 OpenAI messages 中 assistant tool_calls 与 tool result 是否成对且顺序合法。

        规则：
        - assistant 发出 tool_calls 后，后续必须先补齐对应 tool messages
        - tool message 的 tool_call_id 必须来自最近一批未完成的 tool_calls
        - 同一批 tool result 按 tool_calls 顺序返回
        """
        errors: list[str] = []
        orphan_tool_result_ids: list[str] = []
        pending: list[str] = []

        for index, msg in enumerate(messages):
            role = msg.get("role")
            tool_calls = msg.get("tool_calls") or []

            if pending:
                if role != "tool":
                    errors.append(
                        f"message[{index}] role={role} appeared before pending tool results: {pending}"
                    )
                else:
                    tool_call_id = msg.get("tool_call_id")
                    if tool_call_id not in pending:
                        orphan_tool_result_ids.append(str(tool_call_id))
                        errors.append(f"message[{index}] orphan tool result: {tool_call_id}")
                    elif tool_call_id != pending[0]:
                        errors.append(
                            f"message[{index}] tool result order mismatch: expected {pending[0]}, got {tool_call_id}"
                        )
                        pending.remove(tool_call_id)
                    else:
                        pending.pop(0)
                continue

            if role == "assistant" and tool_calls:
                pending = [call["id"] for call in tool_calls]
                continue

            if role == "tool":
                tool_call_id = msg.get("tool_call_id")
                orphan_tool_result_ids.append(str(tool_call_id))
                errors.append(f"message[{index}] tool result without pending tool call: {tool_call_id}")

        if pending:
            errors.append(f"dangling tool calls: {pending}")

        return MessageValidationResult(
            ok=not errors,
            errors=errors,
            pending_tool_call_ids=pending,
            orphan_tool_result_ids=orphan_tool_result_ids,
        )

    def mark_failed(self, call_id: str, code: str, error: str, hint: str | None = None) -> dict[str, Any]:
        """
        给已有 tool_call 补一个错误 result，保证 tool_call/result 链路不断裂。
        这里后续可以优化，不仅仅是补一个错误的result，还可以做有限度的重试/纠错
        """
        result = {
            "ok": False,
            "code": code,
            "data": None,
            "error": error,
            "hint": hint,
        }
        self.add_result(call_id, result)
        return result

    def repair_pending(self, code: str, error: str, hint: str | None = None) -> list[ToolStep]:
        """
        将没有 result 的 tool_call 标记为失败。

        这是链路修复，不是工具重试：目标是保证已经出现的 tool_call
        一定有对应 tool result，避免后续 messages 协议不完整。
        """
        repaired = []
        for step in self.pending_calls():
            self.mark_failed(step.call_id, code, error, hint)
            repaired.append(step)
        return repaired

    def to_tool_messages(
        self,
        encoder: Callable[[dict[str, Any]], str],
        steps: Iterable[ToolStep] | None = None,
    ) -> list[dict[str, str]]:
        """
        导出可写入messages的工具链，
        感觉对checkpoint或trace追踪有好处
        感觉后续优化后可以替代封装loop里的拼接逻辑
        """
        messages = []
        target_steps = self.steps if steps is None else steps
        for step in target_steps:
            if step.result is None:
                continue
            messages.append({
                "tool_call_id": step.call_id,
                "content": encoder(step.result),
            })
        return messages
