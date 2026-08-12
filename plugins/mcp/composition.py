from __future__ import annotations

from dataclasses import dataclass

from core.agent_workspace import AgentWorkspace
from plugins.mcp.application import McpApplicationService
from plugins.mcp.client_manager import McpClientManager
from plugins.mcp.content import McpContentService
from plugins.mcp.registry import McpRegistry
from plugins.mcp.secrets import McpSecretStore


@dataclass
class McpPlugin:
    service: McpApplicationService
    client_manager: McpClientManager

    @property
    def toolset_provider(self):
        return self.service.toolset_provider


def create_mcp_plugin(
    agent_workspace: AgentWorkspace,
    *,
    client_manager: McpClientManager | None = None,
) -> McpPlugin:
    registry = McpRegistry.from_agent_workspace(agent_workspace)
    secret_store = McpSecretStore.from_agent_workspace(agent_workspace)
    manager = client_manager or McpClientManager(secret_store)
    content = McpContentService(registry, manager)
    service = McpApplicationService(
        registry,
        secret_store,
        manager,
        content_service=content,
    )
    return McpPlugin(service=service, client_manager=manager)
