from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.tools_runtime.tools_state import ToolStep, ToolsState

WRITE_TOOL_NAMES = frozenset({"apply_patch", "replace_all", "write_file"})
VERIFY_TOOL_NAMES = frozenset({"get_changes"})
COMMAND_WRITE_RESULT_CODES = frozenset({"COMMAND_COMPLETED", "COMMAND_TIMEOUT"})


@dataclass(frozen=True)
class StopSafety:
    business_safe: bool
    reason: str | None = None

    @property
    def can_stop(self) -> bool:
        return self.business_safe


def _requires_verification(step: ToolStep) -> bool:
    result_data = (step.result or {}).get("data") or {}
    return (
        step.name in WRITE_TOOL_NAMES
        and step.ok is True
    ) or (
        step.name == "execute_command"
        and step.code in COMMAND_WRITE_RESULT_CODES
        and result_data.get("workspace_effect", "may_write") == "may_write"
    )


def successful_writes(tools_state: ToolsState) -> list[ToolStep]:
    return [
        step
        for step in tools_state.steps
        if _requires_verification(step)
    ]


def verified_after_last_write(tools_state: ToolsState) -> bool:
    last_write_index = None
    for index, step in enumerate(tools_state.steps):
        if _requires_verification(step):
            last_write_index = index

    if last_write_index is None:
        return True

    return any(
        step.name in VERIFY_TOOL_NAMES and step.ok is True
        for step in tools_state.steps[last_write_index + 1:]
    )


def needs_verification(tools_state: ToolsState) -> bool:
    return bool(successful_writes(tools_state)) and not verified_after_last_write(
        tools_state
    )


def verification_status(tools_state: ToolsState) -> dict[str, Any]:
    return {
        "successful_writes": len(successful_writes(tools_state)),
        "verified_after_last_write": verified_after_last_write(tools_state),
        "needs_verification": needs_verification(tools_state),
    }


def evaluate_stop_safety(
    tools_state: ToolsState,
) -> StopSafety:
    if needs_verification(tools_state):
        return StopSafety(
            business_safe=False,
            reason="verification_required",
        )

    return StopSafety(business_safe=True)
