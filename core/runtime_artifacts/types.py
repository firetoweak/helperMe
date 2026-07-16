from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    size_chars: int


@dataclass(frozen=True)
class ArtifactChunk:
    artifact_id: str
    content: str
    offset: int
    next_offset: int | None
    total_chars: int

    @property
    def truncated(self) -> bool:
        return self.next_offset is not None
