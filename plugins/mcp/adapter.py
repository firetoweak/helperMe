from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from mcp.types import (
    AudioContent,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)

from core.tool_registry import JsonSchemaParameters, ToolSpec
from core.tools_runtime.progressive_toolsets import ToolsetLoadError
from plugins.mcp.models import sanitize_error_summary


_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
_MAX_TOOL_NAME_LENGTH = 64


def encode_tool_name(server_id: str, tool_name: str) -> str:
    """稳定编码跨 Server 的扁平工具名。"""
    raw = f"mcp__{server_id}__{tool_name}"
    cleaned = _SAFE_NAME.sub("_", raw)
    if len(cleaned) <= _MAX_TOOL_NAME_LENGTH:
        return cleaned
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    keep = _MAX_TOOL_NAME_LENGTH - 9
    return f"{cleaned[:keep]}_{digest}"


def toolset_id_for(server_id: str) -> str:
    return f"mcp:{server_id}"


def parse_toolset_id(toolset_id: str) -> str:
    prefix = "mcp:"
    if not toolset_id.startswith(prefix):
        raise ToolsetLoadError(
            "TOOLSET_NOT_FOUND",
            f"Toolset {toolset_id} not found",
            data={"toolset_id": toolset_id},
        )
    server_id = toolset_id[len(prefix) :]
    if not server_id:
        raise ToolsetLoadError(
            "TOOLSET_NOT_FOUND",
            f"Toolset {toolset_id} not found",
            data={"toolset_id": toolset_id},
        )
    return server_id


def build_parameters(tool_name: str, input_schema: Mapping[str, Any]) -> JsonSchemaParameters:
    try:
        return JsonSchemaParameters(input_schema)
    except Exception as exc:
        raise ToolsetLoadError(
            "MCP_INVALID_TOOL_SCHEMA",
            f"工具 {tool_name} 的 inputSchema 非法: {exc}",
            hint="请修复 MCP Server 的工具 Schema 后重试 load_toolset。",
            data={"tool_name": tool_name},
        ) from exc


def adapt_call_result(
    result: CallToolResult,
    *,
    output_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    content = [_serialize_content_block(block) for block in result.content]
    structured = result.structuredContent
    meta = result.meta

    if output_schema is not None and structured is not None:
        try:
            JsonSchemaParameters(
                _object_schema_for_structured(output_schema)
            ).validate({"value": structured} if output_schema.get("type") != "object" else structured)
        except Exception:
            # outputSchema 可能不是 object；用 jsonschema 直接校验 structured。
            try:
                from jsonschema import validate

                validate(instance=structured, schema=dict(output_schema))
            except Exception as exc:
                return {
                    "ok": False,
                    "code": "MCP_INVALID_TOOL_RESULT",
                    "data": {
                        "mcp": {
                            "content": content,
                            "structured_content": structured,
                            "meta": meta,
                        }
                    },
                    "error": f"structuredContent 不符合 outputSchema: {exc}",
                    "hint": "请检查 MCP Server 返回值或 outputSchema。",
                }

    if result.isError:
        return {
            "ok": False,
            "code": "MCP_TOOL_ERROR",
            "data": {
                "mcp": {
                    "content": content,
                    "structured_content": structured,
                    "meta": meta,
                }
            },
            "error": _text_from_content(content) or "MCP tool reported an error",
            "hint": "可根据服务端返回修正参数后重试。",
        }

    return {
        "ok": True,
        "code": "MCP_TOOL_OK",
        "data": {
            "mcp": {
                "content": content,
                "structured_content": structured,
                "meta": meta,
            }
        },
    }


def adapt_transport_error(exc: BaseException) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "MCP_TRANSPORT_ERROR",
        "data": {},
        "error": sanitize_error_summary(str(exc) or exc.__class__.__name__),
        "hint": "检查 Server 是否可用、地址/命令是否正确，稍后重试。",
    }


def adapt_protocol_error(exc: BaseException) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "MCP_PROTOCOL_ERROR",
        "data": {},
        "error": sanitize_error_summary(str(exc) or exc.__class__.__name__),
        "hint": "检查 MCP Server 协议兼容性后重试。",
    }


def input_required_unsupported() -> dict[str, Any]:
    return {
        "ok": False,
        "code": "MCP_INPUT_REQUIRED_UNSUPPORTED",
        "data": {},
        "error": "当前 MVP 不支持 MCP input_required / multi-round-trip 请求",
        "hint": "请改用无需中途交互的工具，或等待后续能力。",
    }


def _serialize_content_block(block: Any) -> dict[str, Any]:
    if isinstance(block, TextContent):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageContent):
        return {
            "type": "image",
            "mimeType": block.mimeType,
            "data": block.data,
        }
    if isinstance(block, AudioContent):
        return {
            "type": "audio",
            "mimeType": block.mimeType,
            "data": block.data,
        }
    if isinstance(block, ResourceLink):
        return {
            "type": "resource_link",
            "name": block.name,
            "uri": str(block.uri),
            "description": block.description,
            "mimeType": block.mimeType,
        }
    if isinstance(block, EmbeddedResource):
        resource = block.resource
        payload: dict[str, Any] = {
            "type": "resource",
            "uri": str(resource.uri),
            "mimeType": getattr(resource, "mimeType", None),
        }
        if hasattr(resource, "text"):
            payload["text"] = resource.text
        if hasattr(resource, "blob"):
            payload["blob"] = resource.blob
        return payload
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", by_alias=True)
    return {"type": "unknown", "repr": repr(block)}


def _text_from_content(content: list[dict[str, Any]]) -> str:
    texts = [
        item["text"]
        for item in content
        if item.get("type") == "text" and item.get("text")
    ]
    return "\n".join(texts)


def _object_schema_for_structured(schema: Mapping[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "object":
        return dict(schema)
    return {
        "type": "object",
        "properties": {"value": dict(schema)},
        "required": ["value"],
    }


def ensure_unique_encoded_names(specs: list[ToolSpec]) -> None:
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ToolsetLoadError(
            "MCP_TOOL_NAME_CONFLICT",
            "编码后的工具名在同一 Toolset 内冲突",
            data={"names": names},
        )
