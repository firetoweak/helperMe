from __future__ import annotations

from dataclasses import dataclass

from helperme.paths import HelperMeHome
from helperme.mcp.application import McpApplicationService
from helperme.mcp.client_manager import McpClientManager
from helperme.mcp.content import McpContentService
from helperme.mcp.registry import McpRegistry
from helperme.mcp.secrets import McpSecretStore
from helperme.mcp.approval import (
    McpInstallApprovalHandler,
    McpRecoveryApprovalHandler,
    McpUpdateApprovalHandler,
    create_mcp_install_proposal_spec,
    create_mcp_recovery_proposal_spec,
    create_mcp_update_proposal_spec,
)
from helperme.mcp.management_tools import create_mcp_management_specs
from helperme.tools.control import ControlOperation
from helperme.tools.spec import ToolSpec


@dataclass
class McpAssembly:
    service: McpApplicationService
    client_manager: McpClientManager
    management_specs: tuple[ToolSpec, ...]
    control_operations: tuple[ControlOperation, ...]

    @property
    def toolset_provider(self):
        return self.service.toolset_provider


def build_mcp(
    home: HelperMeHome,
    *,
    client_manager: McpClientManager | None = None,
) -> McpAssembly:
    registry = McpRegistry.from_home(home)
    secret_store = McpSecretStore.from_home(home)
    manager = (
        McpClientManager(
            secret_store,
            runtime_root=home.mcp_root / "runtime",
        )
        if client_manager is None
        else client_manager
    )
    content = McpContentService(registry, manager)
    service = McpApplicationService(
        registry,
        secret_store,
        manager,
        content_service=content,
    )
    control_operations = (
        ControlOperation(
            "mcp",
            create_mcp_install_proposal_spec(service),
            McpInstallApprovalHandler(service),
        ),
        ControlOperation(
            "mcp",
            create_mcp_recovery_proposal_spec(service),
            McpRecoveryApprovalHandler(service),
        ),
        ControlOperation(
            "mcp",
            create_mcp_update_proposal_spec(service),
            McpUpdateApprovalHandler(service),
        ),
    )
    return McpAssembly(
        service=service,
        client_manager=manager,
        management_specs=create_mcp_management_specs(service),
        control_operations=control_operations,
    )
