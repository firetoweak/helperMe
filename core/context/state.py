from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class ContextState:
    summary: str | None = None
    summarized_through_message_id: str | None = None
    tool_artifacts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_artifacts", dict(self.tool_artifacts))
