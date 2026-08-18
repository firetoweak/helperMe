from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping

from plugins.mcp.client_manager import McpClientManager
from plugins.mcp.content import McpContentService
from plugins.mcp.models import (
    McpServerRecord,
    McpServerRuntimeState,
    RuntimeAvailability,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    TransportKind,
    utc_now,
    validate_server_id,
)
from plugins.mcp.registry import McpRegistry
from plugins.mcp.secrets import McpSecretStore
from plugins.mcp.toolset_provider import McpToolsetProvider


@dataclass(frozen=True)
class ServerSummary:
    record: McpServerRecord
    runtime: McpServerRuntimeState | None = None

    def to_dict(self, *, include_runtime: bool = False) -> dict[str, Any]:
        payload = self.record.to_dict()
        if include_runtime and self.runtime is not None:
            payload["runtime"] = self.runtime.to_dict()
        return payload


@dataclass(frozen=True)
class ServerActivationResult:
    record: McpServerRecord
    runtime: McpServerRuntimeState

    @property
    def succeeded(self) -> bool:
        return self.runtime.status is RuntimeAvailability.AVAILABLE


class McpApplicationService:
    """用户控制面：list/upsert/enable/remove/test。"""

    def __init__(
        self,
        registry: McpRegistry,
        secret_store: McpSecretStore,
        client_manager: McpClientManager,
        content_service: McpContentService | None = None,
    ) -> None:
        self.registry = registry
        self.secret_store = secret_store
        self.client_manager = client_manager
        self.content = content_service or McpContentService(
            registry,
            client_manager,
        )
        self.toolset_provider = McpToolsetProvider(registry, client_manager)
        self._management_lock = asyncio.Lock()

    async def list_servers(
        self,
        *,
        include_runtime: bool = False,
    ) -> list[ServerSummary]:
        records = await self.registry.list_servers()
        return [
            ServerSummary(
                record=record,
                runtime=(
                    self.client_manager.runtime_state(record.id)
                    if include_runtime
                    else None
                ),
            )
            for record in records
        ]

    async def upsert_server(
        self,
        *,
        server_id: str,
        display_name: str,
        description: str = "",
        transport: str,
        transport_config: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
        enabled: bool = False,
    ) -> McpServerRecord:
        async with self._management_lock:
            return await self._upsert_server_locked(
                server_id=server_id,
                display_name=display_name,
                description=description,
                transport=transport,
                transport_config=transport_config,
                secrets=secrets,
                enabled=enabled,
            )

    async def _upsert_server_locked(
        self,
        *,
        server_id: str,
        display_name: str,
        description: str,
        transport: str,
        transport_config: Mapping[str, Any],
        secrets: Mapping[str, str] | None,
        enabled: bool,
    ) -> McpServerRecord:
        validate_server_id(server_id)
        kind = TransportKind(transport)
        existing = await self.registry.get(server_id)
        prepared_secrets = dict(secrets or {})
        refs: dict[str, str]

        if kind is TransportKind.STDIO:
            prepared_secrets.update(dict(transport_config.get("env") or {}))
            refs = await self._resolve_refs(
                server_id=server_id,
                existing=existing,
                prepared_secrets=prepared_secrets,
                existing_refs=(
                    dict(existing.transport_config.env_refs)
                    if existing is not None
                    and isinstance(existing.transport_config, StdioTransportConfig)
                    else {}
                ),
            )
            config: StdioTransportConfig | StreamableHttpTransportConfig = (
                StdioTransportConfig(
                    command=transport_config["command"],
                    args=tuple(transport_config.get("args") or ()),
                    cwd=transport_config.get("cwd"),
                    env_refs=refs,
                )
            )
        else:
            prepared_secrets.update(dict(transport_config.get("headers") or {}))
            bearer = transport_config.get("bearer")
            if bearer:
                prepared_secrets["Authorization"] = f"Bearer {bearer}"
            refs = await self._resolve_refs(
                server_id=server_id,
                existing=existing,
                prepared_secrets=prepared_secrets,
                existing_refs=(
                    dict(existing.transport_config.header_refs)
                    if existing is not None
                    and isinstance(
                        existing.transport_config,
                        StreamableHttpTransportConfig,
                    )
                    else {}
                ),
            )
            config = StreamableHttpTransportConfig(
                url=transport_config["url"],
                header_refs=refs,
                timeout_seconds=float(
                    transport_config.get("timeout_seconds", 30)
                ),
            )

        updated_secrets = bool(prepared_secrets)
        previous_secrets = self.secret_store.snapshot_namespace(server_id)
        if updated_secrets:
            # 先写 Secret，再原子替换 Registry；失败则恢复旧命名空间。
            refs = self.secret_store.put_namespace(server_id, prepared_secrets)
            if kind is TransportKind.STDIO:
                assert isinstance(config, StdioTransportConfig)
                config = StdioTransportConfig(
                    command=config.command,
                    args=config.args,
                    cwd=config.cwd,
                    env_refs=refs,
                )
            else:
                assert isinstance(config, StreamableHttpTransportConfig)
                config = StreamableHttpTransportConfig(
                    url=config.url,
                    header_refs=refs,
                    timeout_seconds=config.timeout_seconds,
                )

        record = McpServerRecord(
            id=server_id,
            display_name=display_name,
            description=description,
            transport=kind,
            transport_config=config,
            enabled=enabled,
            created_at=existing.created_at if existing is not None else utc_now(),
            updated_at=utc_now(),
        )
        try:
            stored = await self.registry.upsert(record)
        except Exception:
            if updated_secrets:
                if previous_secrets:
                    self.secret_store.put_namespace(server_id, previous_secrets)
                else:
                    self.secret_store.delete_namespace(server_id)
            raise
        await self.client_manager.invalidate(server_id)
        return stored

    async def set_server_enabled(
        self,
        server_id: str,
        enabled: bool,
    ) -> McpServerRecord:
        async with self._management_lock:
            record = await self.registry.set_enabled(server_id, enabled)
            await self.client_manager.invalidate(server_id)
            return record

    async def remove_server(self, server_id: str) -> McpServerRecord:
        async with self._management_lock:
            record = await self.registry.remove(server_id)
            self.secret_store.delete_namespace(server_id)
            await self.client_manager.invalidate(server_id)
            return record

    async def test_server(self, server_id: str) -> McpServerRuntimeState:
        record = await self.registry.get(server_id)
        if record is None:
            raise KeyError(server_id)
        return await self._test_record(record)

    async def test_and_enable(
        self,
        server_id: str,
        *,
        expected_revision: int | None = None,
    ) -> ServerActivationResult:
        """测试冻结配置，并仅在测试成功后原子推进到 enabled。"""
        async with self._management_lock:
            record = await self.registry.get(server_id)
            if record is None:
                raise KeyError(server_id)
            if (
                expected_revision is not None
                and record.revision != expected_revision
            ):
                raise ValueError(
                    f"MCP Server `{server_id}` 配置已变化："
                    f"expected revision {expected_revision}, "
                    f"current revision {record.revision}"
                )
            runtime = await self._test_record(record)
            if runtime.status is not RuntimeAvailability.AVAILABLE:
                return ServerActivationResult(record, runtime)
            enabled = await self.registry.set_enabled(server_id, True)
            await self.client_manager.invalidate(server_id)
            return ServerActivationResult(enabled, runtime)

    async def _test_record(
        self,
        record: McpServerRecord,
    ) -> McpServerRuntimeState:
        try:
            return await self.client_manager.test_connection(record)
        except Exception as exc:
            state = self.client_manager.runtime_state(record.id)
            state.mark_unavailable(
                self.client_manager.sanitized_error(record, exc)
            )
            return state

    async def _resolve_refs(
        self,
        *,
        server_id: str,
        existing: McpServerRecord | None,
        prepared_secrets: Mapping[str, str],
        existing_refs: Mapping[str, str],
    ) -> dict[str, str]:
        if prepared_secrets:
            return {
                key: self.secret_store.ref_for(server_id, key)
                for key in prepared_secrets
            }
        if existing is not None:
            return dict(existing_refs)
        return {}
