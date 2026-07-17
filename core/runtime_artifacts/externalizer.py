from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.runtime_artifacts.store import ArtifactStore
from core.tools_runtime.tools_executor import encode_tool_result


@dataclass(frozen=True)
class ToolResultLimit:
    max_chars: int = 16_000
    preview_chars: int = 1_200

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("max_chars 必须大于 0")
        if not 0 <= self.preview_chars < self.max_chars:
            raise ValueError("preview_chars 必须大于等于 0 且小于 max_chars")


@dataclass(frozen=True)
class ExternalizeOutcome:
    result: dict[str, Any]
    original_chars: int
    projected_chars: int
    externalized: bool


class ToolResultExternalizer:
    def __init__(
        self,
        store: ArtifactStore,
        limit: ToolResultLimit,
    ) -> None:
        self.store = store
        self.limit = limit

    def process(self, result: dict[str, Any]) -> ExternalizeOutcome:
        serialized = encode_tool_result(result)
        original_chars = len(serialized)
        if original_chars <= self.limit.max_chars:
            return ExternalizeOutcome(
                result=result,
                original_chars=original_chars,
                projected_chars=original_chars,
                externalized=False,
            )

        artifact = self.store.save(serialized)
        projected = {
            "ok": result["ok"],
            "code": result["code"],
            "data": {
                "externalized": True,
                "artifact_id": artifact.artifact_id,
                "size_chars": artifact.size_chars,
                "preview": serialized[: self.limit.preview_chars],
            },
            "error": (
                None
                if result["ok"]
                else "完整错误结果已保存为 runtime artifact"
            ),
            "hint": "需要更多内容时调用 read_artifact 分页读取。",
        }
        projected_chars = len(encode_tool_result(projected))
        if projected_chars > self.limit.max_chars:
            raise ValueError("外置后的工具结果仍超过 max_chars")
        return ExternalizeOutcome(
            result=projected,
            original_chars=original_chars,
            projected_chars=projected_chars,
            externalized=True,
        )
