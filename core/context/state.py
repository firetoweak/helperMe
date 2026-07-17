from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class ContextState:
    summary: str | None = None
    summarized_through_message_id: str | None = None
    tool_artifacts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        has_summary = self.summary is not None
        has_boundary = self.summarized_through_message_id is not None
        if has_summary != has_boundary:
            raise ValueError(
                "summary 与 summarized_through_message_id 必须同时存在或同时为空"
            )
        object.__setattr__(self, "tool_artifacts", dict(self.tool_artifacts))
