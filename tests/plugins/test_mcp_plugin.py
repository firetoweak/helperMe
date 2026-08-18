from __future__ import annotations

import unittest
import sys
import asyncio
import json
import socket
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import AsyncMock, patch

from mcp.types import (
    CallToolResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    Prompt,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)

from core.agent_workspace import AgentWorkspace
from core.observability import format_run_log
from core.runtime_artifacts import (
    FileArtifactStore,
    ToolResultExternalizer,
    ToolResultLimit,
)
from core.tool_registry import JsonSchemaParameters
from core.tools_runtime.progressive_toolsets import ToolsetLoadError
from plugins.mcp.adapter import (
    adapt_call_result,
    build_output_validator,
    encode_tool_name,
)
from plugins.mcp.application import McpApplicationService
from plugins.mcp.client_manager import ManagedMcpConnection, McpClientManager
from plugins.mcp.composition import create_mcp_plugin
from plugins.mcp.models import (
    RuntimeAvailability,
    StreamableHttpTransportConfig,
)
from plugins.mcp.registry import McpRegistry
from plugins.mcp.secrets import McpSecretStore


class FakeMcpSession:
    def __init__(
        self,
        *,
        tools: list[Tool] | None = None,
        call_results: dict[str, CallToolResult] | None = None,
        fail_initialize: Exception | None = None,
    ) -> None:
        self.tools = tools or []
        self.call_results = call_results or {}
        self.fail_initialize = fail_initialize
        self.initialized = False
        self.calls: list[tuple[str, dict | None]] = []

    @property
    def protocol_version(self) -> str:
        return "2026-07-28"

    @property
    def server_capabilities(self) -> Any:
        class _Caps:
            def model_dump(self, **kwargs):
                return {"tools": {}}

        return _Caps()

    async def list_tools(self, *, cursor=None) -> ListToolsResult:
        return ListToolsResult(tools=self.tools)

    async def call_tool(self, name: str, arguments=None) -> CallToolResult:
        self.calls.append((name, arguments))
        if name in self.call_results:
            return self.call_results[name]
        return CallToolResult(
            content=[TextContent(type="text", text=f"ok:{name}")],
            isError=False,
        )

    async def list_resources(self, *, cursor=None):
        return ListResourcesResult(resources=[])

    async def list_resource_templates(self, *, cursor=None):
        return ListResourceTemplatesResult(resourceTemplates=[])

    async def read_resource(self, uri):
        raise NotImplementedError

    async def list_prompts(self, *, cursor=None):
        return ListPromptsResult(prompts=[])

    async def get_prompt(self, name, arguments=None):
        raise NotImplementedError


def _tool(
    name: str,
    schema: dict | None = None,
    output_schema: dict | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=f"tool {name}",
        inputSchema=schema
        or {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
        outputSchema=output_schema,
    )


def _runtime_root(workspace: AgentWorkspace) -> Path:
    return workspace.plugins_root / "mcp" / "runtime"


class McpRegistrySecretTest(unittest.IsolatedAsyncioTestCase):
    async def test_registry_and_secrets_roundtrip(self):
        with TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_agent_workspace(workspace)
            secrets = McpSecretStore.from_agent_workspace(workspace)
            refs = secrets.put_namespace("demo", {"TOKEN": "secret-value"})
            service = McpApplicationService(
                registry,
                secrets,
                McpClientManager(
                    secrets,
                    runtime_root=_runtime_root(workspace),
                ),
            )
            record = await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                description="demo server",
                transport="stdio",
                transport_config={
                    "command": "python",
                    "args": ["server.py"],
                    "cwd": str(Path(directory)),
                },
                secrets={"TOKEN": "secret-value"},
                enabled=False,
            )
            self.assertEqual(record.id, "demo")
            self.assertFalse(record.enabled)
            self.assertIn("TOKEN", record.transport_config.env_refs)
            self.assertNotIn(
                "secret-value",
                registry.path.read_text(encoding="utf-8"),
            )
            self.assertEqual(secrets.resolve(refs["TOKEN"]), "secret-value")

            enabled = await service.set_server_enabled("demo", True)
            self.assertTrue(enabled.enabled)
            self.assertEqual(enabled.revision, 2)

            await service.remove_server("demo")
            self.assertEqual(await registry.list_servers(), ())
            self.assertFalse(
                (workspace.plugins_root / "mcp" / "secrets" / "demo.json").exists()
            )

    async def test_http_non_localhost_requires_https(self):
        with self.assertRaises(ValueError):
            StreamableHttpTransportConfig(url="http://example.com/mcp")

    async def test_server_id_rejects_secret_ref_and_path_ambiguity(self):
        with TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            workspace.initialize()
            service = McpApplicationService(
                McpRegistry.from_agent_workspace(workspace),
                McpSecretStore.from_agent_workspace(workspace),
                McpClientManager(
                    McpSecretStore.from_agent_workspace(workspace),
                    runtime_root=_runtime_root(workspace),
                ),
            )
            for invalid_id in ("a:b", "a/b", "a\\b", "..", "含中文"):
                with self.subTest(server_id=invalid_id):
                    with self.assertRaises(ValueError):
                        await service.upsert_server(
                            server_id=invalid_id,
                            display_name="invalid",
                            transport="stdio",
                            transport_config={"command": "python"},
                            secrets={"TOKEN": "value"},
                        )

    async def test_existing_secret_namespace_is_restored_when_registry_write_fails(self):
        with TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_agent_workspace(workspace)
            secrets = McpSecretStore.from_agent_workspace(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            await service.upsert_server(
                server_id="demo",
                display_name="Demo",
                transport="stdio",
                transport_config={"command": "python"},
                secrets={"TOKEN": "old"},
            )

            with patch.object(
                registry,
                "upsert",
                AsyncMock(side_effect=OSError("disk full")),
            ):
                with self.assertRaises(OSError):
                    await service.upsert_server(
                        server_id="demo",
                        display_name="Demo",
                        transport="stdio",
                        transport_config={"command": "python"},
                        secrets={"TOKEN": "new"},
                    )

            self.assertEqual(
                secrets.snapshot_namespace("demo"),
                {"TOKEN": "old"},
            )


class McpAdapterTest(unittest.TestCase):
    def test_encode_tool_name_is_stable_and_namespaced(self):
        self.assertEqual(
            encode_tool_name("github", "search"),
            "mcp__github__search",
        )
        long_name = encode_tool_name("s", "x" * 80)
        self.assertLessEqual(len(long_name), 64)
        self.assertTrue(long_name.startswith("mcp__s__"))

    def test_adapt_call_result_preserves_error_content(self):
        result = CallToolResult(
            content=[TextContent(type="text", text="missing q")],
            isError=True,
        )
        adapted = adapt_call_result(result)
        self.assertFalse(adapted["ok"])
        self.assertEqual(adapted["code"], "MCP_TOOL_ERROR")
        self.assertEqual(adapted["data"]["mcp"]["content"][0]["text"], "missing q")

    def test_output_schema_requires_structured_content(self):
        validator = build_output_validator(
            "demo",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )
        result = CallToolResult(
            content=[TextContent(type="text", text="missing structured")],
            isError=False,
        )
        adapted = adapt_call_result(result, output_validator=validator)
        self.assertFalse(adapted["ok"])
        self.assertEqual(adapted["code"], "MCP_INVALID_TOOL_RESULT")

    def test_tool_error_does_not_need_to_match_output_schema(self):
        validator = build_output_validator(
            "demo",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )
        result = CallToolResult(
            content=[TextContent(type="text", text="business error")],
            isError=True,
        )
        adapted = adapt_call_result(result, output_validator=validator)
        self.assertEqual(adapted["code"], "MCP_TOOL_ERROR")

    def test_adapt_call_result_redacts_secrets_recursively(self):
        secret = "credential-that-must-not-leak"
        result = CallToolResult(
            content=[TextContent(type="text", text=f"token={secret}")],
            structuredContent={
                "token": secret,
                "nested": [f"Bearer {secret}"],
            },
            _meta={"debug": f"header={secret}"},
            isError=False,
        )

        adapted = adapt_call_result(result, secret_values=(secret,))

        serialized = json.dumps(adapted, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertIn("***", serialized)

    def test_redacted_result_cannot_leak_through_artifact_or_log(self):
        secret = "artifact-log-secret"
        result = CallToolResult(
            content=[TextContent(
                type="text",
                text=(f"credential={secret};" + "x" * 500),
            )],
            structuredContent={"credential": secret},
            isError=False,
        )
        adapted = adapt_call_result(result, secret_values=(secret,))

        with TemporaryDirectory() as directory:
            store = FileArtifactStore(Path(directory))
            outcome = ToolResultExternalizer(
                store,
                ToolResultLimit(max_chars=500, preview_chars=80),
            ).process(adapted)
            artifact = store.read(
                outcome.result["data"]["artifact_id"],
                0,
                10_000,
            ).content
            log = format_run_log({
                "started_at": "2026-08-18 00:00:00",
                "ended_at": "2026-08-18 00:00:01",
                "model": "test",
                "run_id": "run",
                "status": "completed",
                "final_reason": None,
                "question": "test",
                "system_prompt": "test",
                "model_requests": [],
                "answer": "done",
                "checkpoints": [outcome.result],
            })

        self.assertTrue(outcome.externalized)
        self.assertNotIn(secret, artifact)
        self.assertNotIn(secret, log)


class McpProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = TemporaryDirectory()
        workspace = AgentWorkspace(Path(self._tmp.name) / ".helperme")
        workspace.initialize()
        self.registry = McpRegistry.from_agent_workspace(workspace)
        self.secrets = McpSecretStore.from_agent_workspace(workspace)
        self.runtime_root = _runtime_root(workspace)
        self.sessions: dict[str, FakeMcpSession] = {}
        self.resolved_secrets: dict[str, dict[str, str]] = {}

        async def factory(record, secrets_map):
            session = self.sessions[record.id]
            self.resolved_secrets[record.id] = dict(secrets_map)
            if session.fail_initialize is not None:
                raise session.fail_initialize
            session.initialized = True
            stack = AsyncExitStack()
            await stack.__aenter__()
            return ManagedMcpConnection(
                session=session,
                stack=stack,
                record=record,
            )

        self.manager = McpClientManager(
            self.secrets,
            runtime_root=self.runtime_root,
            session_factory=factory,
        )
        self.service = McpApplicationService(
            self.registry,
            self.secrets,
            self.manager,
        )

    async def asyncTearDown(self):
        await self.manager.aclose()
        self._tmp.cleanup()

    async def test_descriptors_only_include_enabled_and_avoid_network(self):
        self.sessions["a"] = FakeMcpSession(tools=[_tool("search")])
        await self.service.upsert_server(
            server_id="a",
            display_name="A",
            description="alpha",
            transport="stdio",
            transport_config={"command": "python", "args": ["a.py"]},
            enabled=False,
        )
        await self.service.upsert_server(
            server_id="b",
            display_name="B",
            description="beta",
            transport="stdio",
            transport_config={"command": "python", "args": ["b.py"]},
            enabled=True,
        )
        descriptors = self.service.toolset_provider.descriptors()
        self.assertEqual([item.id for item in descriptors], ["mcp:b"])
        self.assertFalse(self.sessions["a"].initialized)

    async def test_load_toolset_discovers_and_routes_namespaced_tools(self):
        self.sessions["github"] = FakeMcpSession(tools=[_tool("search")])
        self.sessions["jira"] = FakeMcpSession(tools=[_tool("search")])
        for server_id in ("github", "jira"):
            await self.service.upsert_server(
                server_id=server_id,
                display_name=server_id,
                description=server_id,
                transport="stdio",
                transport_config={"command": "python", "args": [f"{server_id}.py"]},
                enabled=True,
            )

        github_specs = await self.service.toolset_provider.tool_specs("mcp:github")
        jira_specs = await self.service.toolset_provider.tool_specs("mcp:jira")
        self.assertEqual(github_specs[0].name, "mcp__github__search")
        self.assertEqual(jira_specs[0].name, "mcp__jira__search")
        self.assertIsInstance(github_specs[0].parameters, JsonSchemaParameters)

        result = await github_specs[0].handler({"q": "mcp"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.sessions["github"].calls, [("search", {"q": "mcp"})])
        self.assertEqual(self.sessions["jira"].calls, [])

    async def test_connection_factory_receives_resolved_secret_values(self):
        self.sessions["secure"] = FakeMcpSession(tools=[_tool("search")])
        await self.service.upsert_server(
            server_id="secure",
            display_name="Secure",
            transport="stdio",
            transport_config={"command": "python"},
            secrets={"TOKEN": "secret-value"},
            enabled=True,
        )
        await self.service.toolset_provider.tool_specs("mcp:secure")
        self.assertEqual(
            self.resolved_secrets["secure"],
            {"TOKEN": "secret-value"},
        )

    async def test_load_failure_marks_runtime_unavailable_but_keeps_catalog(self):
        self.sessions["broken"] = FakeMcpSession(
            fail_initialize=ConnectionError("boom"),
        )
        await self.service.upsert_server(
            server_id="broken",
            display_name="Broken",
            description="broken",
            transport="stdio",
            transport_config={"command": "python", "args": ["x.py"]},
            enabled=True,
        )
        with self.assertRaises(ToolsetLoadError) as ctx:
            await self.service.toolset_provider.tool_specs("mcp:broken")
        self.assertEqual(ctx.exception.code, "MCP_TRANSPORT_ERROR")
        runtime = self.manager.runtime_state("broken")
        self.assertEqual(runtime.status, RuntimeAvailability.UNAVAILABLE)
        descriptors = self.service.toolset_provider.descriptors()
        self.assertEqual(descriptors[0].id, "mcp:broken")
        self.assertIn("最近失败", descriptors[0].description)

    async def test_revision_change_invalidates_client_cache(self):
        self.sessions["demo"] = FakeMcpSession(tools=[_tool("ping")])
        await self.service.upsert_server(
            server_id="demo",
            display_name="Demo",
            description="demo",
            transport="stdio",
            transport_config={"command": "python", "args": ["a.py"]},
            enabled=True,
        )
        old_specs = await self.service.toolset_provider.tool_specs("mcp:demo")
        first_session = self.sessions["demo"]
        self.assertTrue(first_session.initialized)

        replacement = FakeMcpSession(tools=[_tool("ping")])
        self.sessions["demo"] = replacement
        await self.service.upsert_server(
            server_id="demo",
            display_name="Demo2",
            description="demo2",
            transport="stdio",
            transport_config={"command": "python", "args": ["b.py"]},
            enabled=True,
        )
        await self.service.toolset_provider.tool_specs("mcp:demo")
        self.assertTrue(replacement.initialized)
        stale_result = await old_specs[0].handler({"q": "old snapshot"})
        self.assertFalse(stale_result["ok"])
        self.assertEqual(stale_result["code"], "MCP_SERVER_CHANGED")

    async def test_invalid_schema_fails_whole_toolset(self):
        self.sessions["bad"] = FakeMcpSession(
            tools=[
                _tool(
                    "broken",
                    schema={"type": "string"},
                )
            ]
        )
        await self.service.upsert_server(
            server_id="bad",
            display_name="Bad",
            description="bad",
            transport="stdio",
            transport_config={"command": "python", "args": ["bad.py"]},
            enabled=True,
        )
        with self.assertRaises(ToolsetLoadError) as ctx:
            await self.service.toolset_provider.tool_specs("mcp:bad")
        self.assertEqual(ctx.exception.code, "MCP_INVALID_TOOL_SCHEMA")

    async def test_invalid_output_schema_fails_whole_toolset(self):
        self.sessions["bad_output"] = FakeMcpSession(
            tools=[_tool("broken", output_schema={"type": "invalid"})]
        )
        await self.service.upsert_server(
            server_id="bad_output",
            display_name="Bad Output",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )
        with self.assertRaises(ToolsetLoadError) as ctx:
            await self.service.toolset_provider.tool_specs("mcp:bad_output")
        self.assertEqual(ctx.exception.code, "MCP_INVALID_TOOL_SCHEMA")

    async def test_content_service_uses_v2_resource_and_prompt_fields(self):
        session = FakeMcpSession()
        session.list_resources = AsyncMock(
            return_value=ListResourcesResult(
                resources=[
                    Resource(
                        name="guide",
                        uri="file:///guide.md",
                        mimeType="text/markdown",
                    )
                ],
                nextCursor="resource-next",
            )
        )
        session.list_resource_templates = AsyncMock(
            return_value=ListResourceTemplatesResult(
                resourceTemplates=[
                    ResourceTemplate(
                        name="user",
                        uriTemplate="user://{id}",
                        mimeType="application/json",
                    )
                ]
            )
        )
        session.list_prompts = AsyncMock(
            return_value=ListPromptsResult(
                prompts=[Prompt(name="review", description="review code")]
            )
        )
        self.sessions["content"] = session
        await self.service.upsert_server(
            server_id="content",
            display_name="Content",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )

        resources = await self.service.content.list_resources("content")
        templates = await self.service.content.list_resource_templates("content")
        prompts = await self.service.content.list_prompts("content")
        self.assertEqual(resources["next_cursor"], "resource-next")
        self.assertEqual(resources["resources"][0]["mimeType"], "text/markdown")
        self.assertEqual(
            templates["resource_templates"][0]["uriTemplate"],
            "user://{id}",
        )
        self.assertEqual(prompts["prompts"][0]["name"], "review")

    async def test_connection_is_closed_when_post_connect_metadata_fails(self):
        closed = False

        class BrokenMetadataSession(FakeMcpSession):
            @property
            def server_capabilities(self):
                raise RuntimeError("metadata broken")

        async def factory(record, secrets_map):
            nonlocal closed
            stack = AsyncExitStack()
            await stack.__aenter__()

            async def mark_closed():
                nonlocal closed
                closed = True

            stack.push_async_callback(mark_closed)
            return ManagedMcpConnection(
                session=BrokenMetadataSession(),
                stack=stack,
                record=record,
            )

        manager = McpClientManager(
            self.secrets,
            runtime_root=self.runtime_root,
            session_factory=factory,
        )
        service = McpApplicationService(self.registry, self.secrets, manager)
        await service.upsert_server(
            server_id="metadata",
            display_name="Metadata",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )
        with self.assertRaises(ToolsetLoadError):
            await service.toolset_provider.tool_specs("mcp:metadata")
        self.assertTrue(closed)
        await manager.aclose()

    async def test_cancellation_waits_for_uncached_connection_cleanup(self):
        factory_entered = asyncio.Event()
        allow_factory_return = asyncio.Event()
        closed = asyncio.Event()

        async def factory(record, secrets_map):
            factory_entered.set()
            await allow_factory_return.wait()
            stack = AsyncExitStack()
            await stack.__aenter__()

            async def mark_closed():
                closed.set()

            stack.push_async_callback(mark_closed)
            return ManagedMcpConnection(
                session=FakeMcpSession(tools=[_tool("ping")]),
                stack=stack,
                record=record,
            )

        manager = McpClientManager(
            self.secrets,
            runtime_root=self.runtime_root,
            session_factory=factory,
        )
        service = McpApplicationService(self.registry, self.secrets, manager)
        await service.upsert_server(
            server_id="cancelled",
            display_name="Cancelled",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )
        load_task = asyncio.create_task(
            service.toolset_provider.tool_specs("mcp:cancelled")
        )
        await factory_entered.wait()
        await manager._lock.acquire()
        try:
            allow_factory_return.set()
            await asyncio.sleep(0)
            load_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await load_task
        finally:
            manager._lock.release()
        self.assertTrue(closed.is_set())
        await manager.aclose()

    async def test_disable_during_open_prevents_stale_connection_cache(self):
        factory_entered = asyncio.Event()
        allow_factory_return = asyncio.Event()
        closed = asyncio.Event()

        async def factory(record, secrets_map):
            factory_entered.set()
            await allow_factory_return.wait()
            stack = AsyncExitStack()
            await stack.__aenter__()

            async def mark_closed():
                closed.set()

            stack.push_async_callback(mark_closed)
            return ManagedMcpConnection(
                session=FakeMcpSession(tools=[_tool("ping")]),
                stack=stack,
                record=record,
            )

        manager = McpClientManager(
            self.secrets,
            runtime_root=self.runtime_root,
            session_factory=factory,
        )
        service = McpApplicationService(self.registry, self.secrets, manager)
        await service.upsert_server(
            server_id="disable_race",
            display_name="Disable race",
            transport="stdio",
            transport_config={"command": "python"},
            enabled=True,
        )
        load_task = asyncio.create_task(
            service.toolset_provider.tool_specs("mcp:disable_race")
        )
        await factory_entered.wait()
        await service.set_server_enabled("disable_race", False)
        allow_factory_return.set()
        with self.assertRaises(ToolsetLoadError):
            await load_task
        self.assertTrue(closed.is_set())
        self.assertNotIn("disable_race", manager._connections)
        await manager.aclose()

    async def test_create_mcp_plugin_wires_application_resource(self):
        plugin = create_mcp_plugin(
            AgentWorkspace(Path(self._tmp.name) / ".helperme2"),
        )
        async with plugin.client_manager:
            self.assertIsNotNone(plugin.toolset_provider)


class McpRealStdioIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_v2_stdio_reuses_state_across_toolset_loads(self):
        with TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_agent_workspace(workspace)
            secrets = McpSecretStore.from_agent_workspace(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            fixture = (
                Path(__file__).parents[1]
                / "fixtures"
                / "mcp_stdio_server.py"
            )
            await service.upsert_server(
                server_id="real_stdio",
                display_name="Real stdio",
                transport="stdio",
                transport_config={
                    "command": sys.executable,
                    "args": [str(fixture)],
                },
                secrets={"MCP_TEST_TOKEN": "stdio-secret"},
                enabled=True,
            )
            try:
                specs = await service.toolset_provider.tool_specs(
                    "mcp:real_stdio"
                )
                token_spec = next(
                    spec
                    for spec in specs
                    if spec.name.endswith("read_test_token")
                )
                result = await token_spec.handler({})
                self.assertTrue(result["ok"])
                self.assertEqual(
                    result["data"]["mcp"]["structured_content"]["token"],
                    "***",
                )
                counter_spec = next(
                    spec
                    for spec in specs
                    if spec.name.endswith("increment_counter")
                )
                first = await counter_spec.handler({})
                reloaded_specs = await service.toolset_provider.tool_specs(
                    "mcp:real_stdio"
                )
                reloaded_counter_spec = next(
                    spec
                    for spec in reloaded_specs
                    if spec.name.endswith("increment_counter")
                )
                second = await reloaded_counter_spec.handler({})
                self.assertEqual(
                    first["data"]["mcp"]["structured_content"]["count"],
                    1,
                )
                self.assertEqual(
                    second["data"]["mcp"]["structured_content"]["count"],
                    2,
                )
                cwd_spec = next(
                    spec
                    for spec in specs
                    if spec.name.endswith("read_working_directory")
                )
                cwd_result = await cwd_spec.handler({})
                expected_cwd = _runtime_root(workspace) / "real_stdio"
                self.assertEqual(
                    Path(
                        cwd_result["data"]["mcp"]["structured_content"]["cwd"]
                    ).resolve(),
                    expected_cwd.resolve(),
                )
                self.assertTrue(expected_cwd.is_dir())
                self.assertEqual(
                    manager.runtime_state("real_stdio").negotiated_version,
                    "2026-07-28",
                )
            finally:
                await manager.aclose()


class McpRealStreamableHttpIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    async def test_real_streamable_http_lists_and_calls_tool(self):
        with TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_agent_workspace(workspace)
            secrets = McpSecretStore.from_agent_workspace(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            fixture = (
                Path(__file__).parents[1]
                / "fixtures"
                / "mcp_streamable_http_server.py"
            )
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(fixture),
                "--port",
                str(port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await service.upsert_server(
                server_id="real_http",
                display_name="Real HTTP",
                transport="streamable_http",
                transport_config={
                    "url": f"http://127.0.0.1:{port}/mcp",
                    "timeout_seconds": 2,
                },
                enabled=True,
            )
            try:
                specs = None
                for _ in range(30):
                    if process.returncode is not None:
                        stderr = (await process.stderr.read()).decode(
                            errors="replace"
                        )
                        self.fail(f"HTTP fixture exited early: {stderr}")
                    try:
                        specs = await service.toolset_provider.tool_specs(
                            "mcp:real_http"
                        )
                        break
                    except ToolsetLoadError:
                        await asyncio.sleep(0.1)
                self.assertIsNotNone(specs)
                echo = next(spec for spec in specs if spec.name.endswith("echo"))
                result = await echo.handler({"value": "hello-http"})
                self.assertTrue(result["ok"])
                self.assertEqual(
                    result["data"]["mcp"]["structured_content"]["value"],
                    "hello-http",
                )
                self.assertEqual(
                    manager.runtime_state("real_http").negotiated_version,
                    "2026-07-28",
                )
            finally:
                await manager.aclose()
                if process.returncode is None:
                    process.terminate()
                await process.wait()

    async def test_tool_list_collects_all_paginated_pages(self):
        pages = {
            None: ListToolsResult(
                tools=[_tool(f"tool_{index}") for index in range(50)],
                nextCursor="page-2",
                ttlMs=5_000,
            ),
            "page-2": ListToolsResult(
                tools=[_tool(f"tool_{index}") for index in range(50, 120)],
                ttlMs=3_000,
            ),
        }

        class PaginatedSession(FakeMcpSession):
            async def list_tools(self, *, cursor=None):
                return pages[cursor]

        with TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            workspace.initialize()
            manager = McpClientManager(
                McpSecretStore.from_agent_workspace(workspace),
                runtime_root=_runtime_root(workspace),
            )
            tools, ttl = await manager._paginate_tools(PaginatedSession())

        self.assertEqual(len(tools), 120)
        self.assertEqual(tools[-1].name, "tool_119")
        self.assertEqual(ttl, 3.0)


class McpRealStdioCompatibilityIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    async def test_v2_stdio_preserves_explicit_working_directory(self):
        with TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            workspace.initialize()
            explicit_cwd = Path(directory) / "explicit-mcp-cwd"
            explicit_cwd.mkdir()
            registry = McpRegistry.from_agent_workspace(workspace)
            secrets = McpSecretStore.from_agent_workspace(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            fixture = (
                Path(__file__).parents[1]
                / "fixtures"
                / "mcp_stdio_server.py"
            )
            await service.upsert_server(
                server_id="explicit_stdio",
                display_name="Explicit stdio",
                transport="stdio",
                transport_config={
                    "command": sys.executable,
                    "args": [str(fixture)],
                    "cwd": str(explicit_cwd),
                },
                enabled=True,
            )
            try:
                specs = await service.toolset_provider.tool_specs(
                    "mcp:explicit_stdio"
                )
                cwd_spec = next(
                    spec
                    for spec in specs
                    if spec.name.endswith("read_working_directory")
                )
                result = await cwd_spec.handler({})
                self.assertEqual(
                    Path(
                        result["data"]["mcp"]["structured_content"]["cwd"]
                    ).resolve(),
                    explicit_cwd.resolve(),
                )
            finally:
                await manager.aclose()

    async def test_v2_client_falls_back_to_legacy_initialize(self):
        with TemporaryDirectory() as directory:
            workspace = AgentWorkspace(Path(directory) / ".helperme")
            workspace.initialize()
            registry = McpRegistry.from_agent_workspace(workspace)
            secrets = McpSecretStore.from_agent_workspace(workspace)
            manager = McpClientManager(
                secrets,
                runtime_root=_runtime_root(workspace),
            )
            service = McpApplicationService(registry, secrets, manager)
            fixture = (
                Path(__file__).parents[1]
                / "fixtures"
                / "mcp_legacy_stdio_server.py"
            )
            await service.upsert_server(
                server_id="legacy_stdio",
                display_name="Legacy stdio",
                transport="stdio",
                transport_config={
                    "command": sys.executable,
                    "args": [str(fixture)],
                },
                enabled=True,
            )
            try:
                specs = await service.toolset_provider.tool_specs(
                    "mcp:legacy_stdio"
                )
                self.assertEqual(specs, ())
                self.assertEqual(
                    manager.runtime_state("legacy_stdio").negotiated_version,
                    "2025-11-25",
                )
            finally:
                await manager.aclose()


class McpProgressiveIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_load_toolset_failure_does_not_snapshot(self):
        from pydantic import BaseModel

        from core.tools_runtime.progressive_toolsets import (
            ToolsetDescriptor,
            ToolsetLoadingState,
            create_load_toolset_spec,
        )

        class FailingProvider:
            def descriptors(self):
                return (ToolsetDescriptor("mcp:x", "x"),)

            async def tool_specs(self, toolset_id: str):
                raise ToolsetLoadError("MCP_TRANSPORT_ERROR", "down")

        state = ToolsetLoadingState()
        spec = create_load_toolset_spec(
            (ToolsetDescriptor("mcp:x", "x"),),
            state,
            FailingProvider(),
        )

        class Input(BaseModel):
            toolset_id: str

        result = await spec.handler(Input(toolset_id="mcp:x"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "MCP_TRANSPORT_ERROR")
        self.assertEqual(state.loaded_specs, {})


if __name__ == "__main__":
    unittest.main()
