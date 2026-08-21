from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from agent_runtime.events import Event


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    digest: str

    def __post_init__(self) -> None:
        if type(self.digest) is not str or not self.digest:
            raise ValueError("artifact digest must be a non-empty str")


@dataclass(frozen=True, slots=True)
class ArtifactResolution:
    refs: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


class ArtifactStore(Protocol):
    def put(self, content: bytes) -> ArtifactRef:
        ...

    def get(self, ref: ArtifactRef) -> bytes | None:
        ...


class MemoryArtifactStore:
    def __init__(self, blobs: Mapping[str, bytes] | None = None) -> None:
        self._blobs = dict(blobs or {})

    def put(self, content: bytes) -> ArtifactRef:
        if type(content) is not bytes:
            raise TypeError("artifact content must be bytes")
        digest = sha256(content).hexdigest()
        self._blobs[digest] = content
        return ArtifactRef(digest)

    def get(self, ref: ArtifactRef) -> bytes | None:
        if type(ref) is not ArtifactRef:
            raise TypeError("artifact lookup requires ArtifactRef")
        return self._blobs.get(ref.digest)


def resolve_artifacts(
    events: tuple[Event, ...],
    store: ArtifactStore,
) -> ArtifactResolution:
    refs = tuple(dict.fromkeys(
        ref
        for event in events
        for ref in event.artifact_refs
    ))
    missing = tuple(
        ref
        for ref in refs
        if store.get(ArtifactRef(ref)) is None
    )
    return ArtifactResolution(refs=refs, missing=missing)
