import json
from typing import Any

from core.tool_registry import ToolArgumentsError, ToolRegistry
from core.approval import ApprovalRequest

RESERVED_KEYS = frozenset({"ok", "code", "data", "error", "hint"})


def _as_str_error(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, str):
        return error
    return json.dumps(error, ensure_ascii=False)


def normalize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """把内部 tool handler 结果整理为统一输出结构。"""
    extra = {k: v for k, v in result.items() if k not in RESERVED_KEYS}

    return {
        "ok": result["ok"],
        "code": result["code"],
        "data": result["data"] if "data" in result else extra or None,
        "error": _as_str_error(result.get("error")),
        "hint": result.get("hint"),
    }


def encode_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


class ToolsExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self,
        tool_name: str,
        tool_arguments: str,
    ) -> dict[str, Any] | ApprovalRequest:
        spec = self.registry.get(tool_name)
        if spec is None:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "TOOL_NOT_FOUND",
                    "data": {"tool_name": tool_name},
                    "error": f"Tool {tool_name} not found",
                    "hint": "确认工具名称是否正确，或换用已注册工具。",
                }
            )

        if not tool_arguments or not tool_arguments.strip():
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "INVALID_JSON",
                    "error": "tool arguments 不能为空；无参工具也必须显式传入 {}",
                    "hint": "传入合法的 JSON object。",
                }
            )

        try:
            payload = json.loads(tool_arguments)
            if not isinstance(payload, dict):
                raise ToolArgumentsError("tool arguments 必须是 JSON object")
            data = spec.parameters.validate(payload)
            result = await spec.handler(data)
            if isinstance(result, ApprovalRequest):
                if not spec.control_boundary:
                    raise ValueError(
                        "普通工具不能返回 ApprovalRequest: "
                        f"{tool_name}"
                    )
                return result
            return normalize_tool_result(result)
        except json.JSONDecodeError as exc:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "INVALID_JSON",
                    "error": f"invalid json: {exc}",
                    "hint": "修正工具 arguments 的 JSON 格式后重试。",
                }
            )
        except ToolArgumentsError as exc:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "VALIDATION_ERROR",
                    "error": exc.details,
                    "hint": "按工具 schema 修正参数后重试。",
                }
            )

    def is_control_boundary(self, tool_name: str) -> bool:
        spec = self.registry.get(tool_name)
        return spec is not None and spec.control_boundary
