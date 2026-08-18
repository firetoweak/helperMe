import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.approval import ApprovalRequest
from core.tool_registry import ToolArgumentsError
from plugins.mcp.approval import (
    MCP_INSTALL_ACTION,
    MCP_RECOVER_ACTION,
    McpInstallApprovalHandler,
    McpRecoveryApprovalHandler,
    create_mcp_install_proposal_spec,
    create_mcp_recovery_proposal_spec,
)
from plugins.mcp.models import McpServerRuntimeState, RuntimeAvailability
from plugins.mcp.console import McpConsoleAdapter


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
            test_and_enable=AsyncMock(),
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
        enabled = SimpleNamespace(
            id="demo",
            revision=2,
        )
        self.service.test_and_enable.return_value = SimpleNamespace(
            succeeded=True,
            runtime=runtime,
            record=enabled,
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
        self.service.test_and_enable.assert_awaited_once_with(
            "demo",
            expected_revision=1,
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
        self.service.test_and_enable.return_value = SimpleNamespace(
            succeeded=False,
            runtime=runtime,
            record=SimpleNamespace(id="demo", revision=1),
        )

        result = await self.handler.execute(self.payload)

        self.assertFalse(result.succeeded)
        self.assertFalse(result.data["enabled"])


class McpRecoveryApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def test_proposal_freezes_disabled_server_revision(self):
        record = SimpleNamespace(
            id="demo",
            display_name="Demo",
            enabled=False,
            revision=4,
        )
        service = SimpleNamespace(
            registry=SimpleNamespace(get=AsyncMock(return_value=record)),
        )
        spec = create_mcp_recovery_proposal_spec(service)
        input_data = spec.parameters.validate({"server_id": "demo"})

        result = await spec.handler(input_data)

        self.assertIsInstance(result, ApprovalRequest)
        self.assertEqual(result.action, MCP_RECOVER_ACTION)
        self.assertEqual(result.payload["server_id"], "demo")
        self.assertEqual(result.payload["expected_revision"], 4)
        self.assertTrue(spec.control_boundary)

    async def test_missing_server_does_not_create_approval(self):
        service = SimpleNamespace(
            registry=SimpleNamespace(get=AsyncMock(return_value=None)),
        )
        spec = create_mcp_recovery_proposal_spec(service)
        input_data = spec.parameters.validate({"server_id": "missing"})

        result = await spec.handler(input_data)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "MCP_SERVER_NOT_FOUND")

    async def test_handler_tests_and_enables_expected_revision(self):
        runtime = McpServerRuntimeState(status=RuntimeAvailability.AVAILABLE)
        enabled = SimpleNamespace(id="demo", revision=5)
        service = SimpleNamespace(
            test_and_enable=AsyncMock(return_value=SimpleNamespace(
                succeeded=True,
                runtime=runtime,
                record=enabled,
            )),
        )
        handler = McpRecoveryApprovalHandler(service)

        result = await handler.execute({
            "server_id": "demo",
            "expected_revision": 4,
        })

        self.assertTrue(result.succeeded)
        service.test_and_enable.assert_awaited_once_with(
            "demo",
            expected_revision=4,
        )
        self.assertTrue(result.data["enabled"])

    async def test_handler_keeps_unavailable_server_disabled(self):
        runtime = McpServerRuntimeState(
            status=RuntimeAvailability.UNAVAILABLE,
            last_error_summary="connection failed",
        )
        service = SimpleNamespace(
            test_and_enable=AsyncMock(return_value=SimpleNamespace(
                succeeded=False,
                runtime=runtime,
                record=SimpleNamespace(id="demo", revision=4),
            )),
        )
        handler = McpRecoveryApprovalHandler(service)

        result = await handler.execute({
            "server_id": "demo",
            "expected_revision": 4,
        })

        self.assertFalse(result.succeeded)
        self.assertFalse(result.data["enabled"])
        self.assertEqual(
            result.data["runtime"]["last_error_summary"],
            "connection failed",
        )


class McpRecoveryConsoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reuses_atomic_test_and_enable_use_case(self):
        runtime = McpServerRuntimeState(status=RuntimeAvailability.AVAILABLE)
        service = SimpleNamespace(
            test_and_enable=AsyncMock(return_value=SimpleNamespace(
                succeeded=True,
                runtime=runtime,
                record=SimpleNamespace(id="demo", revision=5),
            )),
        )
        console = McpConsoleAdapter(service)

        reply = await console.execute_if_handled("/mcp retry demo")

        service.test_and_enable.assert_awaited_once_with("demo")
        self.assertIn("测试并启用成功", reply)
        self.assertIn("/mcp reload", reply)


if __name__ == "__main__":
    unittest.main()
