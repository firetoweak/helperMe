import json
from typing import Any

from helperme.tools.control import ControlApprovalRequest
from helperme.tools.registry import ToolRegistry
from helperme.tools.spec import ToolArgumentsError


RESERVED_KEYS = frozenset({"ok", "code", "data", "error", "hint"})


def _as_str_error(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, str):
        return error
    return json.dumps(error, ensure_ascii=False)


def normalize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    extra = {k: v for k, v in result.items() if k not in RESERVED_KEYS}
    return {
        "ok": result["ok"],
        "code": result["code"],
        "data": result["data"] if "data" in result else extra or None,
        "error": _as_str_error(result.get("error")),
        "hint": result.get("hint"),
    }


class ToolsExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self,
        tool_name: str,
        tool_arguments: str,
    ) -> dict[str, Any] | ControlApprovalRequest:
        spec = self.registry.get(tool_name)
        if spec is None:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "TOOL_NOT_FOUND",
                    "data": {"tool_name": tool_name},
                    "error": f"Tool {tool_name} is not available",
                    "hint": (
                        "只能调用当前 Step 中暴露的精确名称；"
                        "能力来自未加载的 Toolset 时，先调用 load_toolset。"
                    ),
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
        except json.JSONDecodeError as exc:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "INVALID_JSON",
                    "error": f"invalid json: {exc}",
                    "hint": "修正工具 arguments 的 JSON 格式后重试。",
                }
            )

        try:
            if not isinstance(payload, dict):
                raise ToolArgumentsError("tool arguments 必须是 JSON object")
            data = spec.parameters.validate(payload)
        except ToolArgumentsError as exc:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "VALIDATION_ERROR",
                    "error": exc.details,
                    "hint": "按工具 schema 修正参数后重试。",
                }
            )

        result = await spec.handler(data)
        if isinstance(result, ControlApprovalRequest):
            return result
        return normalize_tool_result(result)
