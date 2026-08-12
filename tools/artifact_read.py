from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.runtime_artifacts import (
    ArtifactNotFoundError,
    ArtifactOffsetOutOfRangeError,
    ArtifactStore,
)
from core.tool_registry import ToolSpec, PydanticParameters


class ReadArtifactInput(BaseModel):
    artifact_id: str = Field(description="先前工具结果真实返回的 artifact_id", pattern=r"^art_[0-9a-f]{32}$")
    offset: int = Field(default=0, ge=0, description="从 0 开始的字符偏移；继续读取时传上次的 next_offset")
    limit: int = Field(default=3000, ge=1, le=3000, description="本次最多读取字符数，范围 1 到 3000")


def create_read_artifact_spec(store: ArtifactStore) -> ToolSpec:
    async def read_artifact(raw: ReadArtifactInput) -> dict[str, Any]:
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
        description="""
用途：分页读取因长度限制而外置保存的完整工具结果。
何时使用：先前工具结果返回 artifact_id，且当前摘要不足以完成判断时使用；已有正文足够时不要额外读取，也不能用它代替 Workspace 文件工具。
关键限制：只能使用工具结果真实提供的 artifact_id；offset 是字符偏移，limit 最大为 3000；Artifact 只在所属 Session 抽屉内有效。
失败/截断后：truncated=true 时使用 next_offset 继续；ARTIFACT_NOT_FOUND 时停止猜测 id；ARTIFACT_OFFSET_OUT_OF_RANGE 时依据错误修正 offset。
""".strip(),
        parameters=PydanticParameters(ReadArtifactInput),
        handler=read_artifact,
    )
