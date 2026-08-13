from __future__ import annotations

import asyncio
from typing import Any

import anyio
from mcp.types import InputRequiredResult

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
    build_output_validator,
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
                    revision=record.revision,
                )
            )
        return tuple(descriptors)

    def toolset_snapshot_token(self) -> object:
        return tuple(
            (record.id, record.revision, record.enabled)
            for record in self._read_records_sync()
        )

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
            summary = self._client_manager.sanitized_error(record, exc)
            self._client_manager.runtime_state(server_id).mark_unavailable(summary)
            await self._client_manager.invalidate(server_id)
            raise ToolsetLoadError(
                "MCP_TRANSPORT_ERROR",
                summary,
                hint="检查 Server 是否可用后重试 load_toolset。",
                data={"server_id": server_id},
            ) from exc

        specs: list[ToolSpec] = []
        for tool in tools:
            parameters = build_parameters(tool.name, tool.input_schema)
            encoded = encode_tool_name(record.id, tool.name)
            output_validator = build_output_validator(
                tool.name,
                tool.output_schema,
            )
            handler = self._make_handler(
                record_id=record.id,
                expected_revision=record.revision,
                tool_name=tool.name,
                output_validator=output_validator,
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
        expected_revision: int,
        tool_name: str,
        output_validator: Any | None,
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
            if record.revision != expected_revision:
                return {
                    "ok": False,
                    "code": "MCP_SERVER_CHANGED",
                    "data": {
                        "server_id": record_id,
                        "loaded_revision": expected_revision,
                        "current_revision": record.revision,
                    },
                    "error": "MCP Server 配置已变化，当前 Toolset 快照已过期",
                    "hint": "请在新的 Run 中重新加载该 Toolset。",
                }
            try:
                result = await self._client_manager.call_tool(
                    record,
                    tool_name,
                    arguments,
                )
            except asyncio.CancelledError:
                raise
            except (
                TimeoutError,
                OSError,
                anyio.EndOfStream,
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
            ) as exc:
                summary = self._client_manager.sanitized_error(record, exc)
                self._client_manager.runtime_state(record_id).mark_unavailable(
                    summary
                )
                await self._client_manager.invalidate(record_id)
                return adapt_transport_error(exc, error_summary=summary)
            except Exception as exc:
                # SDK/协议层错误
                message = self._client_manager.sanitized_error(record, exc)
                if "input_required" in message.lower():
                    return input_required_unsupported()
                return adapt_protocol_error(exc, error_summary=message)

            if isinstance(result, InputRequiredResult):
                return input_required_unsupported()

            return adapt_call_result(
                result,
                output_validator=output_validator,
            )

        return handler

    def _read_enabled_records_sync(self) -> tuple[McpServerRecord, ...]:
        return tuple(
            record for record in self._read_records_sync() if record.enabled
        )

    def _read_records_sync(self) -> tuple[McpServerRecord, ...]:
        # descriptors() 必须同步且禁止网络。直接读取 Registry 文件。
        if not self._registry.path.exists():
            return ()
        import json

        payload = json.loads(self._registry.path.read_text(encoding="utf-8"))
        servers = payload.get("servers", payload)
        records = [
            McpServerRecord.from_dict(item)
            for item in servers
        ]
        return tuple(sorted(records, key=lambda item: item.id))
