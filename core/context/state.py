from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextState:
    summary: str | None = None
    compacted_through_message_id: str | None = None

    def __post_init__(self) -> None:
        has_summary = self.summary is not None
        has_boundary = self.compacted_through_message_id is not None
        if has_summary != has_boundary:
            raise ValueError(
                "summary 与 compacted_through_message_id 必须同时存在或同时为空"
            )