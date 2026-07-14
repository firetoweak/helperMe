from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass
class ToolStep:
    call_id: str
    name: str
    arguments: str
    result: dict[str, Any] | None = None

    @property
    def ok(self) -> bool | None:
        return None if self.result is None else self.result.get("ok")

    @property
    def code(self) -> str | None:
        return None if self.result is None else self.result.get("code")

    @property
    def error(self) -> str | None:
        return None if self.result is None else self.result.get("error")


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
        if step.result is not None:
            raise ValueError(f"duplicate tool result: {call_id}")
        step.result = result

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

    def summary(self) -> dict[str, Any]:
        pending = self.pending_calls()
        return {
            "total": len(self.steps),
            "pending": len(pending),
            "failed": len([step for step in self.steps if step.ok is False]),
        }

    def mark_failed(
        self,
        call_id: str,
        code: str,
        error: str,
        hint: str | None = None,
    ) -> None:
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
