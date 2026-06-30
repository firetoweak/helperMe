import json
from typing import Any

from pydantic import ValidationError

from core.tool_registry import TOOL_SPECS

RESERVED_KEYS = frozenset({"ok", "code", "data", "error", "hint", "message"})


def _as_str_error(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, str):
        return error
    return json.dumps(error, ensure_ascii=False)


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """将任意工具 handler 返回值规范为统一协议。"""
    if isinstance(result, dict) and set(result.keys()) <= RESERVED_KEYS and "ok" in result:
        ok = bool(result["ok"])
        return {
            "ok": ok,
            "code": str(result.get("code") or ("OK" if ok else "ERROR")),
            "data": result.get("data"),
            "error": _as_str_error(result.get("error")),
            "hint": result.get("hint"),
        }

    if isinstance(result, dict) and "ok" in result:
        ok = bool(result["ok"])
        extra = {k: v for k, v in result.items() if k not in RESERVED_KEYS}
        return {
            "ok": ok,
            "code": str(result.get("code") or ("OK" if ok else "ERROR")),
            "data": extra or None,
            "error": _as_str_error(result.get("error")),
            "hint": result.get("hint") or result.get("message"),
        }

    if isinstance(result, dict) and "error" in result:
        extra = {k: v for k, v in result.items() if k not in ("error", "code", "hint", "message")}
        return {
            "ok": False,
            "code": str(result.get("code") or "ERROR"),
            "data": extra or None,
            "error": _as_str_error(result["error"]),
            "hint": result.get("hint") or result.get("message"),
        }

    if isinstance(result, dict):
        return {
            "ok": True,
            "code": "OK",
            "data": result,
            "error": None,
            "hint": None,
        }

    return {
        "ok": True,
        "code": "OK",
        "data": {"value": result},
        "error": None,
        "hint": None,
    }


def _encode_result(result: Any) -> str:
    return json.dumps(normalize_tool_result(result), ensure_ascii=False)


def execute_tool(tool_name: str, tool_arguments: str) -> str:
    spec = TOOL_SPECS.get(tool_name)
    if spec is None:
        return _encode_result(
            {
                "ok": False,
                "code": "TOOL_NOT_FOUND",
                "data": {"tool_name": tool_name},
                "error": f"Tool {tool_name} not found",
                "hint": "确认工具名称是否正确，或换用已注册工具。",
            }
        )

    try:
        payload = json.loads(tool_arguments or "{}")
        data = spec.input_model.model_validate(payload)
        result = spec.handler(data)
        return _encode_result(result)
    except json.JSONDecodeError as exc:
        return _encode_result(
            {
                "ok": False,
                "code": "INVALID_JSON",
                "error": f"invalid json: {exc}",
                "hint": "修正工具 arguments 的 JSON 格式后重试。",
            }
        )
    except ValidationError as exc:
        return _encode_result(
            {
                "ok": False,
                "code": "VALIDATION_ERROR",
                "error": exc.errors(),
                "hint": "按工具 schema 修正参数后重试。",
            }
        )
    except Exception as exc:
        return _encode_result(
            {
                "ok": False,
                "code": "UNHANDLED_ERROR",
                "error": str(exc),
                "hint": "根据 error 调整策略后重试，或换用其他工具。",
            }
        )
