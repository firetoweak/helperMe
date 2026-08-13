import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.approval import ApprovalRequest
from core.tool_registry import ToolArgumentsError
from plugins.mcp.approval import (
    MCP_INSTALL_ACTION,
    McpInstallApprovalHandler,
    create_mcp_install_proposal_spec,
)
from plugins.mcp.models import McpServerRuntimeState, RuntimeAvailability


class McpInstallProposalTest(unittest.IsolatedAsyncioTestCase):
    async def test_agent_constructs_frozen_stdio_proposal(self):
        spec = create_mcp_install_proposal_spec()
        input_data = spec.parameters.validate({
            "server_id": "filesystem",
            "display_name": "Filesystem MCP",
            "description": "读取用户授权目录",
            "transport": "stdio",
            "command": "npx",
            "args": [
                "-y",
                "@modelcontextprotocol/server-filesystem",
                "E:\\myCard",
            ],
            "source": "official_documentation",
        })

        result = await spec.handler(input_data)

        self.assertIsInstance(result, ApprovalRequest)
        self.assertEqual(result.action, MCP_INSTALL_ACTION)
        self.assertTrue(spec.control_boundary)
        self.assertEqual(
            result.payload["transport_config"],
            {
                "command": "npx",
                "args": (
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    "E:\\myCard",
                ),
                "cwd": None,
            },
        )
        self.assertNotIn("secrets", result.payload)
        self.assertIn("Executable：npx", result.summary)

    async def test_rejects_shell_and_unverified_model_inference(self):
        spec = create_mcp_install_proposal_spec()
        base = {
            "server_id": "demo",
            "display_name": "Demo",
            "transport": "stdio",
            "command": "cmd.exe",
            "args": ["/c", "npm install x && x"],
            "source": "user_input",
        }
        with self.assertRaises(ToolArgumentsError):
            spec.parameters.validate(base)

        base["command"] = "npx"
        base["source"] = "model_inference"
        with self.assertRaises(ToolArgumentsError):
            spec.parameters.validate(base)

    async def test_rejects_secret_fields(self):
        spec = create_mcp_install_proposal_spec()
        with self.assertRaises(ToolArgumentsError):
            spec.parameters.validate({
                "server_id": "remote",
                "display_name": "Remote",
                "transport": "streamable_http",
                "url": "https://example.com/mcp",
                "source": "user_input",
                "headers": {"Authorization": "secret"},
            })


class McpInstallApprovalHandlerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(
            upsert_server=AsyncMock(),
            test_server=AsyncMock(),
            set_server_enabled=AsyncMock(),
        )
        self.handler = McpInstallApprovalHandler(self.service)
        self.payload = {
            "server_id": "demo",
            "display_name": "Demo",
            "description": "demo server",
            "transport": "stdio",
            "transport_config": {
                "command": "python",
                "args": ["server.py"],
                "cwd": None,
            },
            "source": "user_input",
        }

    async def test_registers_disabled_then_tests_then_enables(self):
        self.service.upsert_server.return_value = SimpleNamespace(
            id="demo",
            revision=1,
        )
        runtime = McpServerRuntimeState(status=RuntimeAvailability.AVAILABLE)
        self.service.test_server.return_value = runtime
        self.service.set_server_enabled.return_value = SimpleNamespace(
            id="demo",
            revision=2,
        )

        result = await self.handler.execute(self.payload)

        self.assertTrue(result.succeeded)
        self.service.upsert_server.assert_awaited_once_with(
            server_id="demo",
            display_name="Demo",
            description="demo server",
            transport="stdio",
            transport_config=self.payload["transport_config"],
            enabled=False,
        )
        self.service.test_server.assert_awaited_once_with("demo")
        self.service.set_server_enabled.assert_awaited_once_with(
            "demo",
            True,
        )
        self.assertEqual(result.data["revision"], 2)

    async def test_failed_test_keeps_server_disabled(self):
        self.service.upsert_server.return_value = SimpleNamespace(
            id="demo",
            revision=1,
        )
        runtime = McpServerRuntimeState(
            status=RuntimeAvailability.UNAVAILABLE,
            last_error_summary="connection failed",
        )
        self.service.test_server.return_value = runtime

        result = await self.handler.execute(self.payload)

        self.assertFalse(result.succeeded)
        self.service.set_server_enabled.assert_not_awaited()
        self.assertFalse(result.data["enabled"])


if __name__ == "__main__":
    unittest.main()
