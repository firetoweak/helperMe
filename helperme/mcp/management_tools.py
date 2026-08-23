from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from helperme.tools.spec import EmptyInput, PydanticParameters, ToolSpec
from helperme.mcp.application import McpApplicationService
from helperme.mcp.models import RuntimeAvailability


class McpServerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str


def create_mcp_management_specs(
    service: McpApplicationService,
) -> tuple[ToolSpec, ...]:
    async def list_servers(_input: EmptyInput) -> dict:
        items = await service.list_servers(include_runtime=True)
        servers = []
        for item in items:
            record = item.record
            runtime = item.runtime
            servers.append({
                "id": record.id,
                "display_name": record.display_name,
                "description": record.description,
                "transport": record.transport.value,
                "enabled": record.enabled,
                "revision": record.revision,
                "runtime": runtime.to_dict() if runtime is not None else None,
                "recovery": (
                    None
                    if record.enabled
                    else "调用 test_mcp_server 诊断；可用时调用 propose_mcp_recovery。"
                ),
            })
        return {
            "ok": True,
            "code": "MCP_SERVERS_LISTED",
            "data": {"servers": servers},
            "error": None,
            "hint": None,
        }

    async def test_server(input_data: McpServerInput) -> dict:
        record = await service.registry.get(input_data.server_id)
        if record is None:
            return {
                "ok": False,
                "code": "MCP_SERVER_NOT_FOUND",
                "data": {"server_id": input_data.server_id},
                "error": f"未注册 MCP Server `{input_data.server_id}`",
                "hint": "先调用 list_mcp_servers 核对精确 ID。",
            }
        runtime = await service.test_server(record.id)
        available = runtime.status is RuntimeAvailability.AVAILABLE
        if available and not record.enabled:
            code = "MCP_SERVER_READY_TO_ENABLE"
            hint = "Server 已安装且连接可用；需要恢复时调用 propose_mcp_recovery。"
        elif available:
            code = "MCP_SERVER_AVAILABLE"
            hint = "Server 已启用；若 Toolset 不可见，请检查 Session 能力快照。"
        else:
            code = "MCP_SERVER_UNAVAILABLE"
            hint = "依据 runtime.last_error_summary 修正配置后重试；不要重新安装同名 Server。"
        return {
            "ok": available,
            "code": code,
            "data": {
                "server_id": record.id,
                "enabled": record.enabled,
                "revision": record.revision,
                "runtime": runtime.to_dict(),
                "recoverable": not record.enabled,
                "next_action": (
                    "propose_mcp_recovery"
                    if available and not record.enabled
                    else None
                ),
            },
            "error": None if available else runtime.last_error_summary,
            "hint": hint,
        }

    return (
        ToolSpec(
            name="list_mcp_servers",
            description=(
                "列出所有已注册 MCP Server，包括 disabled 项及最近运行状态。"
                "用户询问某个 MCP 是否安装、可用或可恢复时，应先调用本工具；"
                "Toolset 目录只包含 enabled 项，不能代替管理状态。"
            ),
            parameters=PydanticParameters(EmptyInput),
            handler=list_servers,
        ),
        ToolSpec(
            name="test_mcp_server",
            description=(
                "按 Registry 中冻结的配置真实测试一个 MCP Server，disabled 项也可测试。"
                "失败只证明本次连接不可用，不代表 Server 未安装；"
                "应依据 code、hint 和 next_action 继续纠正。"
            ),
            parameters=PydanticParameters(McpServerInput),
            handler=test_server,
        ),
    )
