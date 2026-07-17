from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


ROLE_KEYS = ("system", "user", "assistant", "tool")

# 脱水反事实：把 tool content 换成可回读 stub 后的估算口径（非真实投影）。
DEHYDRATED_TOOL_STUB_TEMPLATE = {
    "ok": True,
    "code": "OK",
    "data": {
        "externalized": True,
        "artifact_id": "art_placeholder",
        "size_chars": 0,
        "preview": "",
    },
    "error": None,
    "hint": "需要更多内容时调用 read_artifact 分页读取。",
}


def empty_role_tokens() -> dict[str, int]:
    return {role: 0 for role in ROLE_KEYS}


def empty_role_counts() -> dict[str, int]:
    return {role: 0 for role in ROLE_KEYS}


def content_char_length(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    return len(
        json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def parse_tool_result_meta(content: Any) -> tuple[bool, str | None]:
    """从 tool content 解析是否已外置及 artifact_id。"""
    payload: Any = content
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return False, None
    if not isinstance(payload, dict):
        return False, None
    data = payload.get("data")
    if not isinstance(data, dict):
        return False, None
    artifact_id = data.get("artifact_id")
    externalized = bool(data.get("externalized")) or isinstance(
        artifact_id, str
    )
    if isinstance(artifact_id, str):
        return externalized, artifact_id
    return externalized, None


def dehydrated_tool_content(size_chars: int, artifact_id: str | None) -> str:
    stub = {
        **DEHYDRATED_TOOL_STUB_TEMPLATE,
        "data": {
            **DEHYDRATED_TOOL_STUB_TEMPLATE["data"],
            "artifact_id": artifact_id or "art_placeholder",
            "size_chars": size_chars,
        },
    }
    return json.dumps(stub, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class ToolResultStat:
    """ModelContext 中单条 tool 消息的可观测拆解。"""

    message_index: int
    tool_call_id: str | None
    tool_name: str | None
    chars: int
    estimated_tokens: int
    externalized: bool
    artifact_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolResultWindowStats:
    """Conversation 事实轨迹上的近期保护窗 vs 可压缩区 tool 体积。"""

    recent_start_message_id: str | None
    recent_protection_tokens: int
    recent_tool_chars: int
    compressible_tool_chars: int
    recent_tool_tokens_estimate: int
    compressible_tool_tokens_estimate: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContextComposition:
    """一次模型请求的上下文构成（估算口径，非 API 真实 usage）。"""

    estimated_total_tokens: int
    input_budget_tokens: int
    tools_schema_tokens: int
    by_role_tokens: dict[str, int]
    by_role_message_counts: dict[str, int]
    tool_result_chars: int
    tool_results: tuple[ToolResultStat, ...] = ()
    dehydrated_tool_tokens_estimate: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimated_total_tokens": self.estimated_total_tokens,
            "input_budget_tokens": self.input_budget_tokens,
            "tools_schema_tokens": self.tools_schema_tokens,
            "by_role_tokens": self.by_role_tokens,
            "by_role_message_counts": self.by_role_message_counts,
            "tool_result_chars": self.tool_result_chars,
            "tool_results": [item.to_dict() for item in self.tool_results],
            "dehydrated_tool_tokens_estimate": (
                self.dehydrated_tool_tokens_estimate
            ),
            "dehydrated_tool_savings_estimate": (
                max(
                    0,
                    self.by_role_tokens.get("tool", 0)
                    - self.dehydrated_tool_tokens_estimate,
                )
            ),
        }


def stub_composition(
    estimated_total_tokens: int,
    input_budget_tokens: int,
) -> ContextComposition:
    """测试或仅有总量时的占位构成。"""
    return ContextComposition(
        estimated_total_tokens=estimated_total_tokens,
        input_budget_tokens=input_budget_tokens,
        tools_schema_tokens=0,
        by_role_tokens=empty_role_tokens(),
        by_role_message_counts=empty_role_counts(),
        tool_result_chars=0,
    )


def empty_tool_window_stats(
    recent_protection_tokens: int = 8_000,
) -> ToolResultWindowStats:
    """测试占位：无 tool 窗口占用。"""
    return ToolResultWindowStats(
        recent_start_message_id=None,
        recent_protection_tokens=recent_protection_tokens,
        recent_tool_chars=0,
        compressible_tool_chars=0,
        recent_tool_tokens_estimate=0,
        compressible_tool_tokens_estimate=0,
    )


def collect_tool_call_names(
    messages: list[dict[str, Any]],
) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            function = call.get("function") or {}
            name = function.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                names[call_id] = name
    return names
