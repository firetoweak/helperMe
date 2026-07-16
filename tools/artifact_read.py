from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.runtime_artifacts import (
    ArtifactNotFoundError,
    ArtifactOffsetOutOfRangeError,
    ArtifactStore,
)
from core.tool_registry import ToolSpec


class ReadArtifactInput(BaseModel):
    artifact_id: str = Field(pattern=r"^art_[0-9a-f]{32}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=3000, ge=1, le=3000)


def create_read_artifact_spec(store: ArtifactStore) -> ToolSpec:
    def read_artifact(raw: ReadArtifactInput) -> dict[str, Any]:
        try:
            chunk = store.read(raw.artifact_id, raw.offset, raw.limit)
        except ArtifactNotFoundError:
            return {
                "ok": False,
                "code": "ARTIFACT_NOT_FOUND",
                "error": f"runtime artifact 不存在: {raw.artifact_id}",
            }
        except ArtifactOffsetOutOfRangeError as exc:
            return {
                "ok": False,
                "code": "ARTIFACT_OFFSET_OUT_OF_RANGE",
                "error": str(exc),
            }

        return {
            "ok": True,
            "code": "ARTIFACT_READ",
            "data": {
                "artifact_id": chunk.artifact_id,
                "content": chunk.content,
                "offset": chunk.offset,
                "next_offset": chunk.next_offset,
                "total_chars": chunk.total_chars,
                "truncated": chunk.truncated,
            },
        }

    return ToolSpec(
        name="read_artifact",
        description=(
            "分页读取已外置的完整工具结果。只能使用工具结果提供的 "
            "artifact_id；offset 是字符偏移，返回 next_offset 时可继续读取。"
        ),
        input_model=ReadArtifactInput,
        handler=read_artifact,
    )
