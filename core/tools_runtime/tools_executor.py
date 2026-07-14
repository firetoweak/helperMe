import json
from typing import Any

from pydantic import ValidationError

from core.tool_registry import TOOL_SPECS

RESERVED_KEYS = frozenset({"ok", "code", "data", "error", "hint"})


def _as_str_error(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, str):
        return error
    return json.dumps(error, ensure_ascii=False)


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """校验并规范工具 handler 的显式结果协议。"""
    if not isinstance(result, dict):
        return _invalid_tool_result("handler 返回值必须是 dict")
    if type(result.get("ok")) is not bool:
        return _invalid_tool_result("handler 必须显式返回布尔字段 ok")
    if not isinstance(result.get("code"), str) or not result["code"].strip():
        return _invalid_tool_result("handler 必须显式返回非空字符串字段 code")

    extra = {k: v for k, v in result.items() if k not in RESERVED_KEYS}
    if "data" in result and extra:
        return _invalid_tool_result("handler 不能同时返回 data 和顶层扩展字段")

    return {
        "ok": result["ok"],
        "code": result["code"],
        "data": result.get("data", extra or None),
        "error": _as_str_error(result.get("error")),
        "hint": result.get("hint"),
    }


def _invalid_tool_result(reason: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "INVALID_TOOL_RESULT",
        "data": None,
        "error": reason,
        "hint": "修正工具 handler，使其显式返回合法的 ok/code 结果协议。",
    }


def encode_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def execute_tool(tool_name: str, tool_arguments: str) -> dict[str, Any]:
    spec = TOOL_SPECS.get(tool_name)
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
        data = spec.input_model.model_validate(payload)
        result = spec.handler(data)
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
    except ValidationError as exc:
        return normalize_tool_result(
            {
                "ok": False,
                "code": "VALIDATION_ERROR",
                "error": exc.errors(),
                "hint": "按工具 schema 修正参数后重试。",
            }
        )
    except Exception as exc:
        return normalize_tool_result(
            {
                "ok": False,
                "code": "UNHANDLED_ERROR",
                "error": str(exc),
                "hint": "根据 error 调整策略后重试，或换用其他工具。",
            }
        )
