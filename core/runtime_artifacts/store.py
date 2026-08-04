from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil
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


class FileArtifactDrawers:
    """以 Session 为边界管理彼此隔离的 ArtifactStore。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def for_session(self, session_id: str) -> FileArtifactStore:
        return FileArtifactStore(self._drawer_path(session_id) / "artifacts")

    def delete(self, session_id: str) -> None:
        shutil.rmtree(self._drawer_path(session_id))

    def _drawer_path(self, session_id: str) -> Path:
        drawer_id = sha256(session_id.encode("utf-8")).hexdigest()
        return self._root / drawer_id
