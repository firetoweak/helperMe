from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    InputRequiredResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    ReadResourceResult,
    Tool,
)

from plugins.mcp.models import (
    McpServerRecord,
    McpServerRuntimeState,
    StdioTransportConfig,
    StreamableHttpTransportConfig,
    TransportKind,
    sanitize_error_summary,
)
from plugins.mcp.secrets import McpSecretStore


async def _finish_cleanup(cleanup: Awaitable[None]) -> None:
    """即使外层任务正在取消，也等待资源清理真正结束。"""
    task = asyncio.create_task(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task


class McpSession(Protocol):
    @property
    def protocol_version(self) -> str | None:
        ...

    @property
    def server_capabilities(self) -> Any:
        ...

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult:
        ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        ...

    async def list_resources(
        self,
        *,
        cursor: str | None = None,
    ) -> ListResourcesResult:
        ...

    async def list_resource_templates(
        self,
        *,
        cursor: str | None = None,
    ) -> ListResourceTemplatesResult:
        ...

    async def read_resource(self, uri: str) -> ReadResourceResult:
        ...

    async def list_prompts(
        self,
        *,
        cursor: str | None = None,
    ) -> ListPromptsResult:
        ...

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> GetPromptResult:
        ...


@dataclass(frozen=True)
class _SdkClientFacade:
    """固定 HelperMe 所需的 v2 Client 子集，并保留 input_required。"""

    client: Client

    @property
    def protocol_version(self) -> str:
        return self.client.protocol_version

    @property
    def server_capabilities(self) -> Any:
        return self.client.server_capabilities

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult:
        return await self.client.list_tools(cursor=cursor, cache_mode="bypass")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        return await self.client.session.call_tool(
            name,
            arguments,
            allow_input_required=True,
        )

    async def list_resources(
        self,
        *,
        cursor: str | None = None,
    ) -> ListResourcesResult:
        return await self.client.list_resources(
            cursor=cursor,
            cache_mode="bypass",
        )

    async def list_resource_templates(
        self,
        *,
        cursor: str | None = None,
    ) -> ListResourceTemplatesResult:
        return await self.client.list_resource_templates(
            cursor=cursor,
            cache_mode="bypass",
        )

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return await self.client.read_resource(uri, cache_mode="bypass")

    async def list_prompts(
        self,
        *,
        cursor: str | None = None,
    ) -> ListPromptsResult:
        return await self.client.list_prompts(
            cursor=cursor,
            cache_mode="bypass",
        )

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> GetPromptResult:
        return await self.client.get_prompt(name, arguments)


SessionFactory = Callable[
    [McpServerRecord, Mapping[str, str]],
    Awaitable["ManagedMcpConnection"],
]


@dataclass
class ManagedMcpConnection:
    session: McpSession
    stack: AsyncExitStack | None
    record: McpServerRecord
    negotiated_version: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    tools_cache: tuple[Tool, ...] | None = None
    tools_cache_expires_at: float | None = None
    cacheable: bool = True
    close_handler: Callable[[], Awaitable[None]] | None = None
    alive_handler: Callable[[], bool] | None = None

    async def aclose(self) -> None:
        if self.close_handler is not None:
            await self.close_handler()
            return
        assert self.stack is not None
        await self.stack.aclose()

    def is_alive(self) -> bool:
        return self.alive_handler is None or self.alive_handler()


@dataclass(frozen=True)
class _SdkOperation:
    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    result: asyncio.Future[Any]


class _OwnedSdkSession:
    """把所有 SDK 调用投递给创建 Client context 的 owner task。"""

    def __init__(
        self,
        owner: "_SdkConnectionOwner",
        *,
        protocol_version: str,
        server_capabilities: Any,
    ) -> None:
        self._owner = owner
        self._protocol_version = protocol_version
        self._server_capabilities = server_capabilities

    @property
    def protocol_version(self) -> str:
        return self._protocol_version

    @property
    def server_capabilities(self) -> Any:
        return self._server_capabilities

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult:
        return await self._owner.call("list_tools", cursor=cursor)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        return await self._owner.call("call_tool", name, arguments)

    async def list_resources(
        self,
        *,
        cursor: str | None = None,
    ) -> ListResourcesResult:
        return await self._owner.call("list_resources", cursor=cursor)

    async def list_resource_templates(
        self,
        *,
        cursor: str | None = None,
    ) -> ListResourceTemplatesResult:
        return await self._owner.call(
            "list_resource_templates",
            cursor=cursor,
        )

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return await self._owner.call("read_resource", uri)

    async def list_prompts(
        self,
        *,
        cursor: str | None = None,
    ) -> ListPromptsResult:
        return await self._owner.call("list_prompts", cursor=cursor)

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> GetPromptResult:
        return await self._owner.call("get_prompt", name, arguments)


class _SdkConnectionOwner:
    """在单一 Task 内拥有真实 MCP SDK Client 及其传输资源。"""

    def __init__(
        self,
        record: McpServerRecord,
        secrets: Mapping[str, str],
    ) -> None:
        self._record = record
        self._secrets = secrets
        self._queue: asyncio.Queue[_SdkOperation | None] = asyncio.Queue()
        self._ready: asyncio.Future[ManagedMcpConnection] = (
            asyncio.get_running_loop().create_future()
        )
        self._task = asyncio.create_task(
            self._run(),
            name=f"mcp-owner:{record.id}",
        )
        self._closing = False

    async def start(self) -> ManagedMcpConnection:
        try:
            return await asyncio.shield(self._ready)
        except BaseException:
            if not self._ready.done():
                self._ready.cancel()
            if not self._task.done():
                self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
            raise

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if not self.is_alive():
            raise ConnectionError(f"MCP connection owner 已停止: {self._record.id}")
        result = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(
            _SdkOperation(
                method=method,
                args=args,
                kwargs=kwargs,
                result=result,
            )
        )
        try:
            return await asyncio.shield(result)
        except asyncio.CancelledError:
            result.cancel()
            self._closing = True
            self._task.cancel()
            raise

    async def close(self) -> None:
        if not self._task.done() and not self._closing:
            self._closing = True
            self._queue.put_nowait(None)
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise

    def is_alive(self) -> bool:
        return not self._closing and not self._task.done()

    async def _run(self) -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        current: _SdkOperation | None = None
        try:
            facade = await self._open_facade(stack)
            connection = ManagedMcpConnection(
                session=_OwnedSdkSession(
                    self,
                    protocol_version=facade.protocol_version,
                    server_capabilities=facade.server_capabilities,
                ),
                stack=None,
                record=self._record,
                negotiated_version=facade.protocol_version,
                close_handler=self.close,
                alive_handler=self.is_alive,
            )
            self._ready.set_result(connection)

            while True:
                current = await self._queue.get()
                if current is None:
                    break
                try:
                    operation = getattr(facade, current.method)
                    value = await operation(*current.args, **current.kwargs)
                except BaseException as exc:
                    if not current.result.done():
                        current.result.set_exception(exc)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                else:
                    if not current.result.done():
                        current.result.set_result(value)
                finally:
                    current = None
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            if current is not None and not current.result.done():
                current.result.set_exception(exc)
            raise
        finally:
            self._closing = True
            self._fail_pending_operations()
            await stack.aclose()

    async def _open_facade(self, stack: AsyncExitStack) -> _SdkClientFacade:
        read_timeout: float | None = None
        if self._record.transport is TransportKind.STDIO:
            assert isinstance(
                self._record.transport_config,
                StdioTransportConfig,
            )
            params = StdioServerParameters(
                command=self._record.transport_config.command,
                args=list(self._record.transport_config.args),
                env=dict(self._secrets) or None,
                cwd=self._record.transport_config.cwd,
            )
            transport = stdio_client(params)
        else:
            assert isinstance(
                self._record.transport_config,
                StreamableHttpTransportConfig,
            )
            read_timeout = self._record.transport_config.timeout_seconds
            http_client = await stack.enter_async_context(
                httpx2.AsyncClient(
                    headers=dict(self._secrets),
                    timeout=read_timeout,
                    follow_redirects=False,
                )
            )
            transport = streamable_http_client(
                self._record.transport_config.url,
                http_client=http_client,
            )
        client = await stack.enter_async_context(
            Client(
                transport,
                mode="auto",
                read_timeout_seconds=read_timeout,
            )
        )
        return _SdkClientFacade(client)

    def _fail_pending_operations(self) -> None:
        error = ConnectionError(f"MCP connection owner 已停止: {self._record.id}")
        while not self._queue.empty():
            operation = self._queue.get_nowait()
            if operation is not None and not operation.result.done():
                operation.result.set_exception(error)


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
        runtime_root: Path,
        session_factory: SessionFactory | None = None,
        list_cache_ttl_seconds: float = 30.0,
    ) -> None:
        self._secret_store = secret_store
        self._runtime_root = runtime_root.resolve()
        self._session_factory = session_factory or self._open_sdk_connection
        self._list_cache_ttl_seconds = list_cache_ttl_seconds
        self._connections: dict[str, _CacheEntry] = {}
        self._generations: dict[str, int] = {}
        self._runtime: dict[str, McpServerRuntimeState] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> "McpClientManager":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def runtime_state(self, server_id: str) -> McpServerRuntimeState:
        return self._runtime.setdefault(server_id, McpServerRuntimeState())

    def sanitized_error(
        self,
        record: McpServerRecord,
        exc: BaseException,
    ) -> str:
        return sanitize_error_summary(
            str(exc) or exc.__class__.__name__,
            secret_values=self.secret_values(record),
        )

    def secret_values(self, record: McpServerRecord) -> tuple[str, ...]:
        try:
            return tuple(
                self._secret_store.resolve_many(record.credential_refs).values()
            )
        except Exception:
            return ()

    async def invalidate(self, server_id: str) -> None:
        async with self._lock:
            self._generations[server_id] = (
                self._generations.get(server_id, 0) + 1
            )
            entry = self._connections.pop(server_id, None)
        if entry is not None:
            await entry.connection.aclose()

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            entries = list(self._connections.values())
            self._connections.clear()
        first_error: BaseException | None = None
        for entry in entries:
            try:
                await entry.connection.aclose()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def test_connection(
        self,
        record: McpServerRecord,
    ) -> McpServerRuntimeState:
        connection = await self._ensure_connection(record, force_refresh=True)
        try:
            return self.runtime_state(record.id)
        finally:
            await self._release_connection(connection)
            if not record.enabled:
                await self.invalidate(record.id)

    async def list_tools(self, record: McpServerRecord) -> tuple[Tool, ...]:
        connection = await self._ensure_connection(record)
        try:
            now = asyncio.get_running_loop().time()
            if (
                connection.tools_cache is not None
                and connection.tools_cache_expires_at is not None
                and connection.tools_cache_expires_at > now
            ):
                return connection.tools_cache
            tools, ttl_seconds = await self._paginate_tools(connection.session)
            connection.tools_cache = tools
            connection.tools_cache_expires_at = now + ttl_seconds
            return tools
        finally:
            await self._release_connection(connection)

    async def call_tool(
        self,
        record: McpServerRecord,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> CallToolResult | InputRequiredResult:
        connection = await self._ensure_connection(record)
        try:
            return await connection.session.call_tool(tool_name, arguments)
        finally:
            await self._release_connection(connection)

    async def list_resources(
        self,
        record: McpServerRecord,
        *,
        cursor: str | None = None,
    ) -> ListResourcesResult:
        connection = await self._ensure_connection(record)
        try:
            return await connection.session.list_resources(cursor=cursor)
        finally:
            await self._release_connection(connection)

    async def list_resource_templates(
        self,
        record: McpServerRecord,
        *,
        cursor: str | None = None,
    ) -> ListResourceTemplatesResult:
        connection = await self._ensure_connection(record)
        try:
            return await connection.session.list_resource_templates(cursor=cursor)
        finally:
            await self._release_connection(connection)

    async def read_resource(
        self,
        record: McpServerRecord,
        uri: str,
    ) -> ReadResourceResult:
        connection = await self._ensure_connection(record)
        try:
            return await connection.session.read_resource(uri)
        finally:
            await self._release_connection(connection)

    async def list_prompts(
        self,
        record: McpServerRecord,
        *,
        cursor: str | None = None,
    ) -> ListPromptsResult:
        connection = await self._ensure_connection(record)
        try:
            return await connection.session.list_prompts(cursor=cursor)
        finally:
            await self._release_connection(connection)

    async def get_prompt(
        self,
        record: McpServerRecord,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> GetPromptResult:
        connection = await self._ensure_connection(record)
        try:
            return await connection.session.get_prompt(name, arguments)
        finally:
            await self._release_connection(connection)

    async def _ensure_connection(
        self,
        record: McpServerRecord,
        *,
        force_refresh: bool = False,
    ) -> ManagedMcpConnection:
        if self._closed:
            raise RuntimeError("McpClientManager 已关闭")
        if not record.enabled and not force_refresh:
            raise PermissionError(f"MCP Server 未启用: {record.id}")

        async with self._lock:
            entry = self._connections.get(record.id)
            if (
                not force_refresh
                and entry is not None
                and entry.revision == record.revision
                and entry.connection.is_alive()
            ):
                return entry.connection
            generation = self._generations.get(record.id, 0)
            old = self._connections.pop(record.id, None)

        if old is not None:
            await old.connection.aclose()

        state = self.runtime_state(record.id)
        connection: ManagedMcpConnection | None = None
        try:
            secrets = self._secret_store.resolve_many(record.credential_refs)
            connection = await self._session_factory(record, secrets)
            capabilities = connection.session.server_capabilities
            capability_payload = (
                capabilities.model_dump(mode="json", exclude_none=True)
                if capabilities is not None and hasattr(capabilities, "model_dump")
                else {}
            )
            negotiated = connection.session.protocol_version
            connection.negotiated_version = negotiated
            connection.capabilities = capability_payload
            connection.record = record
            state.mark_available(
                negotiated_version=negotiated,
                capabilities=capability_payload,
            )
        except asyncio.CancelledError:
            if connection is not None:
                if connection.cacheable:
                    await _finish_cleanup(connection.aclose())
                else:
                    await connection.aclose()
            raise
        except Exception as exc:
            if connection is not None:
                await connection.aclose()
            state.mark_unavailable(self.sanitized_error(record, exc))
            raise

        if not connection.cacheable:
            try:
                async with self._lock:
                    if (
                        self._closed
                        or self._generations.get(record.id, 0) != generation
                    ):
                        raise RuntimeError(
                            "MCP connection invalidated while opening: "
                            f"{record.id}"
                        )
            except BaseException:
                await connection.aclose()
                raise
            return connection

        try:
            async with self._lock:
                if (
                    self._closed
                    or self._generations.get(record.id, 0) != generation
                ):
                    raise RuntimeError(
                        f"MCP connection invalidated while opening: {record.id}"
                    )
                current = self._connections.get(record.id)
                if current is not None and current.revision == record.revision:
                    await connection.aclose()
                    return current.connection
                stale = current
                self._connections[record.id] = _CacheEntry(
                    connection=connection,
                    revision=record.revision,
                )
        except BaseException:
            await _finish_cleanup(connection.aclose())
            raise
        if stale is not None:
            await stale.connection.aclose()
        return connection

    @staticmethod
    async def _release_connection(
        connection: ManagedMcpConnection,
    ) -> None:
        if not connection.cacheable:
            await connection.aclose()

    async def _paginate_tools(
        self,
        session: McpSession,
    ) -> tuple[tuple[Tool, ...], float]:
        tools: list[Tool] = []
        cursor: str | None = None
        ttl_values: list[float] = []
        while True:
            page = await session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if page.ttl_ms is not None:
                ttl_values.append(max(0.0, page.ttl_ms / 1000))
            cursor = page.next_cursor
            if not cursor:
                break
        ttl_seconds = (
            min(ttl_values)
            if ttl_values
            else self._list_cache_ttl_seconds
        )
        return tuple(tools), ttl_seconds

    async def _open_sdk_connection(
        self,
        record: McpServerRecord,
        secrets: Mapping[str, str],
    ) -> ManagedMcpConnection:
        if record.transport is TransportKind.STDIO:
            assert isinstance(record.transport_config, StdioTransportConfig)
            cwd = record.transport_config.cwd
            if cwd is None:
                default_cwd = self._runtime_root / record.id
                default_cwd.mkdir(parents=True, exist_ok=True)
                cwd = str(default_cwd)
            record = replace(
                record,
                transport_config=StdioTransportConfig(
                    command=record.transport_config.command,
                    args=record.transport_config.args,
                    cwd=cwd,
                    env_refs=record.transport_config.env_refs,
                ),
            )
        return await _SdkConnectionOwner(record, secrets).start()
