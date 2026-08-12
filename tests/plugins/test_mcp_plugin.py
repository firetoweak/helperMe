from __future__ import annotations

import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mcp.types import (
    CallToolResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from core.agent_workspace import AgentWorkspace
from core.tool_registry import JsonSchemaParameters
from core.tools_runtime.progressive_toolsets import ToolsetLoadError
from plugins.mcp.adapter import adapt_call_result, encode_tool_name
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

    async def initialize(self) -> Any:
        if self.fail_initialize is not None:
            raise self.fail_initialize
        self.initialized = True

        class _Result:
            protocolVersion = "2025-11-25"

        return _Result()

    def get_server_capabilities(self) -> Any:
        class _Caps:
            def model_dump(self, **kwargs):
                return {"tools": {}}

        return _Caps()

    async def list_tools(self, cursor=None, *, params=None) -> ListToolsResult:
        return ListToolsResult(tools=self.tools)

    async def call_tool(self, name: str, arguments=None) -> CallToolResult:
        self.calls.append((name, arguments))
        if name in self.call_results:
            return self.call_results[name]
        return CallToolResult(
            content=[TextContent(type="text", text=f"ok:{name}")],
            isError=False,
        )

    async def list_resources(self, cursor=None, *, params=None):
        return ListResourcesResult(resources=[])

    async def list_resource_templates(self, cursor=None, *, params=None):
        return ListResourceTemplatesResult(resourceTemplates=[])

    async def read_resource(self, uri):
        raise NotImplementedError

    async def list_prompts(self, cursor=None, *, params=None):
        return ListPromptsResult(prompts=[])

    async def get_prompt(self, name, arguments=None):
        raise NotImplementedError


def _tool(name: str, schema: dict | None = None) -> Tool:
    return Tool(
        name=name,
        description=f"tool {name}",
        inputSchema=schema
        or {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    )


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
                McpClientManager(secrets),
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


class McpProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = TemporaryDirectory()
        workspace = AgentWorkspace(Path(self._tmp.name) / ".helperme")
        workspace.initialize()
        self.registry = McpRegistry.from_agent_workspace(workspace)
        self.secrets = McpSecretStore.from_agent_workspace(workspace)
        self.sessions: dict[str, FakeMcpSession] = {}

        async def factory(record, secrets_map):
            session = self.sessions[record.id]
            stack = AsyncExitStack()
            await stack.__aenter__()
            return ManagedMcpConnection(
                session=session,
                stack=stack,
                record=record,
            )

        self.manager = McpClientManager(
            self.secrets,
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
        await self.service.toolset_provider.tool_specs("mcp:demo")
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

    async def test_create_mcp_plugin_wires_application_resource(self):
        plugin = create_mcp_plugin(
            AgentWorkspace(Path(self._tmp.name) / ".helperme2"),
        )
        async with plugin.client_manager:
            self.assertIsNotNone(plugin.toolset_provider)


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
