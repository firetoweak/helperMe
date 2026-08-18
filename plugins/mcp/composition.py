from __future__ import annotations

from dataclasses import dataclass

from core.agent_workspace import AgentWorkspace
from plugins.mcp.application import McpApplicationService
from plugins.mcp.client_manager import McpClientManager
from plugins.mcp.content import McpContentService
from plugins.mcp.registry import McpRegistry
from plugins.mcp.secrets import McpSecretStore
from plugins.mcp.approval import (
    McpInstallApprovalHandler,
    McpRecoveryApprovalHandler,
    create_mcp_install_proposal_spec,
    create_mcp_recovery_proposal_spec,
)
from plugins.mcp.management_tools import create_mcp_management_specs
from core.tool_registry import ToolSpec


@dataclass
class McpPlugin:
    service: McpApplicationService
    client_manager: McpClientManager
    install_proposal_spec: ToolSpec
    install_approval_handler: McpInstallApprovalHandler
    management_specs: tuple[ToolSpec, ...]
    recovery_proposal_spec: ToolSpec
    recovery_approval_handler: McpRecoveryApprovalHandler

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
    manager = client_manager or McpClientManager(
        secret_store,
        runtime_root=agent_workspace.plugins_root / "mcp" / "runtime",
    )
    content = McpContentService(registry, manager)
    service = McpApplicationService(
        registry,
        secret_store,
        manager,
        content_service=content,
    )
    return McpPlugin(
        service=service,
        client_manager=manager,
        install_proposal_spec=create_mcp_install_proposal_spec(),
        install_approval_handler=McpInstallApprovalHandler(service),
        management_specs=create_mcp_management_specs(service),
        recovery_proposal_spec=create_mcp_recovery_proposal_spec(service),
        recovery_approval_handler=McpRecoveryApprovalHandler(service),
    )
