from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


ROLE_KEYS = ("system", "user", "assistant", "tool")


def empty_role_tokens() -> dict[str, int]:
    return {role: 0 for role in ROLE_KEYS}


def empty_role_counts() -> dict[str, int]:
    return {role: 0 for role in ROLE_KEYS}


@dataclass(frozen=True)
class ContextComposition:
    """一次模型请求的上下文构成（估算口径，非 API 真实 usage）。"""

    estimated_total_tokens: int
    input_budget_tokens: int
    tools_schema_tokens: int
    by_role_tokens: dict[str, int]
    by_role_message_counts: dict[str, int]
    tool_result_chars: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def stub_composition(
    estimated_total_tokens: int,
    input_budget_tokens: int,
) -> ContextComposition:
    """测试或仅有总量时的占位构成。"""
    return ContextComposition(
        estimated_total_tokens=estimated_total_tokens,
        input_budget_tokens=input_budget_tokens,
        tools_schema_tokens=0,
        by_role_tokens=empty_role_tokens(),
        by_role_message_counts=empty_role_counts(),
        tool_result_chars=0,
    )
