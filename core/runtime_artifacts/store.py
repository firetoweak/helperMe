from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import uuid4

from core.runtime_artifacts.types import ArtifactChunk, ArtifactRef


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactOffsetOutOfRangeError(ValueError):
    pass


class ArtifactStore(Protocol):
    def save(self, content: str) -> ArtifactRef:
        ...

    def read(
        self,
        artifact_id: str,
        offset: int,
        limit: int,
    ) -> ArtifactChunk:
        ...


class FileArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, content: str) -> ArtifactRef:
        artifact_id = f"art_{uuid4().hex}"
        self._path(artifact_id).write_text(content, encoding="utf-8")
        return ArtifactRef(
            artifact_id=artifact_id,
            size_chars=len(content),
        )

    def read(
        self,
        artifact_id: str,
        offset: int,
        limit: int,
    ) -> ArtifactChunk:
        path = self._path(artifact_id)
        if not path.is_file():
            raise ArtifactNotFoundError(artifact_id)

        content = path.read_text(encoding="utf-8")
        if offset > len(content):
            raise ArtifactOffsetOutOfRangeError(
                f"offset={offset}, total_chars={len(content)}"
            )

        end = min(offset + limit, len(content))
        return ArtifactChunk(
            artifact_id=artifact_id,
            content=content[offset:end],
            offset=offset,
            next_offset=end if end < len(content) else None,
            total_chars=len(content),
        )

    def _path(self, artifact_id: str) -> Path:
        return self._root / f"{artifact_id}.json"
