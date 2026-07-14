from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.tools_runtime.tools_protocol import validate_tool_message_chain
from core.tools_runtime.tools_state import ToolStep, ToolsState

WRITE_TOOL_NAMES = frozenset({"apply_patch", "replace_all", "write_file"})
VERIFY_TOOL_NAMES = frozenset({"get_changes"})


@dataclass(frozen=True)
class StopSafety:
    protocol_safe: bool
    business_safe: bool
    reason: str | None = None

    @property
    def can_stop(self) -> bool:
        return self.protocol_safe and self.business_safe


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
    messages: list[dict[str, Any]],
    tools_state: ToolsState,
) -> StopSafety:
    validation = validate_tool_message_chain(messages)
    if not validation.ok:
        return StopSafety(
            protocol_safe=False,
            business_safe=not needs_verification(tools_state),
            reason="message_chain_invalid",
        )

    if needs_verification(tools_state):
        return StopSafety(
            protocol_safe=True,
            business_safe=False,
            reason="verification_required",
        )

    return StopSafety(protocol_safe=True, business_safe=True)
