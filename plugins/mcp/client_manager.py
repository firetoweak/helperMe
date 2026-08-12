from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceResult,
    Tool,
)
from pydantic import AnyUrl

from plugins.mcp.models import (
    McpServerRecord,
    McpServerRuntimeState,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    TransportKind,
)
from plugins.mcp.secrets import McpSecretStore


class McpSession(Protocol):
    async def initialize(self) -> Any:
        ...

    def get_server_capabilities(self) -> Any:
        ...

    async def list_tools(
        self,
        cursor: str | None = None,
        *,
        params: PaginatedRequestParams | None = None,
    ) -> ListToolsResult:
        ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        ...

    async def list_resources(
        self,
        cursor: str | None = None,
        *,
        params: PaginatedRequestParams | None = None,
    ) -> ListResourcesResult:
        ...

    async def list_resource_templates(
        self,
        cursor: str | None = None,
        *,
        params: PaginatedRequestParams | None = None,
    ) -> ListResourceTemplatesResult:
        ...

    async def read_resource(self, uri: AnyUrl) -> ReadResourceResult:
        ...

    async def list_prompts(
        self,
        cursor: str | None = None,
        *,
        params: PaginatedRequestParams | None = None,
    ) -> ListPromptsResult:
        ...

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> GetPromptResult:
        ...


SessionFactory = Callable[
    [McpServerRecord, Mapping[str, str]],
    Awaitable["ManagedMcpConnection"],
]


@dataclass
class ManagedMcpConnection:
    session: McpSession
    stack: AsyncExitStack
    record: McpServerRecord
    negotiated_version: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    tools_cache: tuple[Tool, ...] | None = None
    tools_cache_expires_at: float | None = None

    async def aclose(self) -> None:
        await self.stack.aclose()


@dataclass
class _CacheEntry:
    connection: ManagedMcpConnection
    revision: int


class McpClientManager:
    """按 (server_id, revision) 懒创建并缓存 MCP Client。"""

    def __init__(
        self,
        secret_store: McpSecretStore,
        *,
        session_factory: SessionFactory | None = None,
        list_cache_ttl_seconds: float = 30.0,
    ) -> None:
        self._secret_store = secret_store
        self._session_factory = session_factory or self._open_sdk_connection
        self._list_cache_ttl_seconds = list_cache_ttl_seconds
        self._connections: dict[str, _CacheEntry] = {}
        self._runtime: dict[str, McpServerRuntimeState] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> "McpClientManager":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def runtime_state(self, server_id: str) -> McpServerRuntimeState:
        return self._runtime.setdefault(server_id, McpServerRuntimeState())

    async def invalidate(self, server_id: str) -> None:
        async with self._lock:
            entry = self._connections.pop(server_id, None)
        if entry is not None:
            await entry.connection.aclose()

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            entries = list(self._connections.values())
            self._connections.clear()
        for entry in entries:
            await entry.connection.aclose()

    async def test_connection(
        self,
        record: McpServerRecord,
    ) -> McpServerRuntimeState:
        connection = await self._ensure_connection(record, force_refresh=True)
        state = self.runtime_state(record.id)
        return state

    async def list_tools(self, record: McpServerRecord) -> tuple[Tool, ...]:
        connection = await self._ensure_connection(record)
        now = asyncio.get_running_loop().time()
        if (
            connection.tools_cache is not None
            and connection.tools_cache_expires_at is not None
            and connection.tools_cache_expires_at > now
        ):
            return connection.tools_cache
        tools = await self._paginate_tools(connection.session)
        connection.tools_cache = tools
        connection.tools_cache_expires_at = now + self._list_cache_ttl_seconds
        return tools

    async def call_tool(
        self,
        record: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> CallToolResult:
        connection = await self._ensure_connection(record)
        return await connection.session.call_tool(tool_name, arguments)

    async def list_resources(
        self,
        record: McpServerRecord,
        *,
        cursor: str | None = None,
    ) -> ListResourcesResult:
        connection = await self._ensure_connection(record)
        if cursor:
            return await connection.session.list_resources(
                params=PaginatedRequestParams(cursor=cursor),
            )
        return await connection.session.list_resources()

    async def list_resource_templates(
        self,
        record: McpServerRecord,
        *,
        cursor: str | None = None,
    ) -> ListResourceTemplatesResult:
        connection = await self._ensure_connection(record)
        if cursor:
            return await connection.session.list_resource_templates(
                params=PaginatedRequestParams(cursor=cursor),
            )
        return await connection.session.list_resource_templates()

    async def read_resource(
        self,
        record: McpServerRecord,
        uri: str,
    ) -> ReadResourceResult:
        connection = await self._ensure_connection(record)
        return await connection.session.read_resource(AnyUrl(uri))

    async def list_prompts(
        self,
        record: McpServerRecord,
        *,
        cursor: str | None = None,
    ) -> ListPromptsResult:
        connection = await self._ensure_connection(record)
        if cursor:
            return await connection.session.list_prompts(
                params=PaginatedRequestParams(cursor=cursor),
            )
        return await connection.session.list_prompts()

    async def get_prompt(
        self,
        record: McpServerRecord,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> GetPromptResult:
        connection = await self._ensure_connection(record)
        return await connection.session.get_prompt(name, arguments)

    async def _ensure_connection(
        self,
        record: McpServerRecord,
        *,
        force_refresh: bool = False,
    ) -> ManagedMcpConnection:
        if self._closed:
            raise RuntimeError("McpClientManager 已关闭")
        if not record.enabled and not force_refresh:
            # test_server 允许对未启用 Server 显式探活；普通调用必须 enabled。
            raise PermissionError(f"MCP Server 未启用: {record.id}")

        async with self._lock:
            entry = self._connections.get(record.id)
            if (
                not force_refresh
                and entry is not None
                and entry.revision == record.revision
            ):
                return entry.connection
            old = self._connections.pop(record.id, None)

        if old is not None:
            await old.connection.aclose()

        state = self.runtime_state(record.id)
        try:
            secrets = self._secret_store.resolve_many(record.credential_refs)
            connection = await self._session_factory(record, secrets)
            init_result = await connection.session.initialize()
            capabilities = connection.session.get_server_capabilities()
            capability_payload = (
                capabilities.model_dump(mode="json", exclude_none=True)
                if capabilities is not None and hasattr(capabilities, "model_dump")
                else {}
            )
            negotiated = getattr(init_result, "protocolVersion", None)
            connection.negotiated_version = negotiated
            connection.capabilities = capability_payload
            connection.record = record
            state.mark_available(
                negotiated_version=negotiated,
                capabilities=capability_payload,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.mark_unavailable(str(exc) or exc.__class__.__name__)
            raise

        async with self._lock:
            current = self._connections.get(record.id)
            if current is not None and current.revision == record.revision:
                await connection.aclose()
                return current.connection
            if current is not None:
                stale = current
                self._connections[record.id] = _CacheEntry(
                    connection=connection,
                    revision=record.revision,
                )
            else:
                stale = None
                self._connections[record.id] = _CacheEntry(
                    connection=connection,
                    revision=record.revision,
                )
        if stale is not None:
            await stale.connection.aclose()
        return connection

    async def _paginate_tools(self, session: McpSession) -> tuple[Tool, ...]:
        tools: list[Tool] = []
        cursor: str | None = None
        while True:
            if cursor:
                page = await session.list_tools(
                    params=PaginatedRequestParams(cursor=cursor),
                )
            else:
                page = await session.list_tools()
            tools.extend(page.tools)
            cursor = page.nextCursor
            if not cursor:
                break
        return tuple(tools)

    async def _open_sdk_connection(
        self,
        record: McpServerRecord,
        secrets: Mapping[str, str],
    ) -> ManagedMcpConnection:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            if record.transport is TransportKind.STDIO:
                assert isinstance(record.transport_config, StdioTransportConfig)
                env = {
                    key: secrets[ref]
                    for key, ref in record.transport_config.env_refs.items()
                }
                params = StdioServerParameters(
                    command=record.transport_config.command,
                    args=list(record.transport_config.args),
                    env=env or None,
                    cwd=record.transport_config.cwd,
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            else:
                assert isinstance(
                    record.transport_config,
                    StreamableHttpTransportConfig,
                )
                headers = {
                    key: secrets[ref]
                    for key, ref in record.transport_config.header_refs.items()
                }
                timeout = record.transport_config.timeout_seconds
                read, write, _session_id = await stack.enter_async_context(
                    streamablehttp_client(
                        record.transport_config.url,
                        headers=headers or None,
                        timeout=timeout,
                    )
                )
            session = await stack.enter_async_context(ClientSession(read, write))
            return ManagedMcpConnection(
                session=session,
                stack=stack,
                record=record,
            )
        except BaseException:
            await stack.aclose()
            raise
