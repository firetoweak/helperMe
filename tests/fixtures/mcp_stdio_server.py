from __future__ import annotations

import os

from mcp.server import MCPServer


server = MCPServer("helperme-test-server")


@server.tool()
def read_test_token() -> dict[str, str]:
    """返回测试进程收到的 MCP_TEST_TOKEN。"""
    return {"token": os.environ.get("MCP_TEST_TOKEN", "")}


if __name__ == "__main__":
    server.run()
