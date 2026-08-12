from __future__ import annotations

import asyncio
from typing import Any

from core.tool_registry import ToolSpec
from core.tools_runtime.progressive_toolsets import (
    ToolsetDescriptor,
    ToolsetLoadError,
)
from plugins.mcp.adapter import (
    adapt_call_result,
    adapt_protocol_error,
    adapt_transport_error,
    build_parameters,
    encode_tool_name,
    ensure_unique_encoded_names,
    input_required_unsupported,
    parse_toolset_id,
)
from plugins.mcp.client_manager import McpClientManager
from plugins.mcp.models import McpServerRecord
from plugins.mcp.registry import McpRegistry


class McpToolsetProvider:
    """目录只读 Registry；真正连接发生在 tool_specs / 工具调用。"""

    def __init__(
        self,
        registry: McpRegistry,
        client_manager: McpClientManager,
    ) -> None:
        self._registry = registry
        self._client_manager = client_manager

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        # 同步接口：直接读盘快照。Registry 的 list 在测试/单线程下可同步读。
        records = self._read_enabled_records_sync()
        descriptors: list[ToolsetDescriptor] = []
        for record in records:
            runtime = self._client_manager.runtime_state(record.id)
            description = f"{record.display_name}: {record.description}".rstrip(": ")
            if runtime.last_error_summary:
                description = (
                    f"{description}（最近失败：{runtime.last_error_summary}）"
                )
            descriptors.append(
                ToolsetDescriptor(
                    id=record.toolset_id,
                    description=description,
                )
            )
        return tuple(descriptors)

    async def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        server_id = parse_toolset_id(toolset_id)
        record = await self._registry.get(server_id)
        if record is None or not record.enabled:
            raise ToolsetLoadError(
                "TOOLSET_NOT_FOUND",
                f"Toolset {toolset_id} not found",
                hint="请从可选 Toolset 目录中选择有效 ID。",
                data={"toolset_id": toolset_id},
            )
        try:
            tools = await self._client_manager.list_tools(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ToolsetLoadError(
                "MCP_TRANSPORT_ERROR",
                str(exc) or exc.__class__.__name__,
                hint="检查 Server 是否可用后重试 load_toolset。",
                data={"server_id": server_id},
            ) from exc

        specs: list[ToolSpec] = []
        for tool in tools:
            parameters = build_parameters(tool.name, tool.inputSchema)
            encoded = encode_tool_name(record.id, tool.name)
            output_schema = tool.outputSchema
            handler = self._make_handler(
                record_id=record.id,
                tool_name=tool.name,
                output_schema=output_schema,
            )
            specs.append(
                ToolSpec(
                    name=encoded,
                    description=tool.description or tool.name,
                    parameters=parameters,
                    handler=handler,
                )
            )
        ensure_unique_encoded_names(specs)
        return tuple(specs)

    def _make_handler(
        self,
        *,
        record_id: str,
        tool_name: str,
        output_schema: dict[str, Any] | None,
    ):
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            record = await self._registry.get(record_id)
            if record is None or not record.enabled:
                return {
                    "ok": False,
                    "code": "MCP_SERVER_DISABLED",
                    "data": {"server_id": record_id},
                    "error": f"MCP Server 不可用或已停用: {record_id}",
                    "hint": "请用户通过 /mcp 重新启用该 Server。",
                }
            try:
                result = await self._client_manager.call_tool(
                    record,
                    tool_name,
                    arguments,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                self._client_manager.runtime_state(record_id).mark_unavailable(
                    str(exc)
                )
                return adapt_transport_error(exc)
            except OSError as exc:
                self._client_manager.runtime_state(record_id).mark_unavailable(
                    str(exc)
                )
                return adapt_transport_error(exc)
            except Exception as exc:
                # SDK/协议层错误
                message = str(exc) or exc.__class__.__name__
                if "input_required" in message.lower():
                    return input_required_unsupported()
                return adapt_protocol_error(exc)

            # 未来 resultType=input_required 时 SDK 可能用字段表达
            result_type = getattr(result, "resultType", None)
            if result_type == "input_required":
                return input_required_unsupported()

            return adapt_call_result(result, output_schema=output_schema)

        return handler

    def _read_enabled_records_sync(self) -> tuple[McpServerRecord, ...]:
        # descriptors() 必须同步且禁止网络。直接读取 Registry 文件。
        if not self._registry.path.exists():
            return ()
        import json

        payload = json.loads(self._registry.path.read_text(encoding="utf-8"))
        servers = payload.get("servers", payload)
        records = [
            McpServerRecord.from_dict(item)
            for item in servers
            if item.get("enabled")
        ]
        return tuple(sorted(records, key=lambda item: item.id))
