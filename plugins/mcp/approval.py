from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any, Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.approval import ApprovalExecution, ApprovalRequest
from core.tool_registry import PydanticParameters, ToolSpec
from plugins.mcp.application import McpApplicationService
from plugins.mcp.models import RuntimeAvailability


MCP_INSTALL_ACTION = "mcp.install"
PROPOSE_MCP_INSTALL = "propose_mcp_install"

_SHELL_EXECUTABLES = {
    "bash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "sh",
    "zsh",
}


class McpInstallProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str
    display_name: str
    description: str = ""
    transport: Literal["stdio", "streamable_http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    url: str | None = None
    timeout_seconds: float = 30.0
    source: Literal["user_input", "official_documentation", "registry"]

    @model_validator(mode="after")
    def validate_transport(self) -> "McpInstallProposalInput":
        if self.transport == "stdio":
            if self.command is None or not self.command.strip():
                raise ValueError("stdio proposal 必须提供 command")
            if self.url is not None:
                raise ValueError("stdio proposal 不能提供 url")
            executable = PurePath(self.command).name.lower()
            if executable in _SHELL_EXECUTABLES:
                raise ValueError("MCP stdio 不接受 Shell 解释器作为 command")
            if "\n" in self.command or "\r" in self.command:
                raise ValueError("stdio command 不能包含换行")
            if any("\n" in arg or "\r" in arg for arg in self.args):
                raise ValueError("stdio args 不能包含换行")
        else:
            if self.url is None or not self.url.strip():
                raise ValueError("streamable_http proposal 必须提供 url")
            if self.command is not None or self.args or self.cwd is not None:
                raise ValueError(
                    "streamable_http proposal 不能提供 command/args/cwd"
                )
        return self

    def transport_config(self) -> dict[str, Any]:
        if self.transport == "stdio":
            return {
                "command": self.command,
                "args": list(self.args),
                "cwd": self.cwd,
            }
        return {
            "url": self.url,
            "timeout_seconds": self.timeout_seconds,
        }

    def frozen_payload(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "display_name": self.display_name,
            "description": self.description,
            "transport": self.transport,
            "transport_config": self.transport_config(),
            "source": self.source,
        }

    def approval_summary(self) -> str:
        lines = [
            f"准备安装 MCP Server `{self.server_id}`（{self.display_name}）",
            f"Transport：{self.transport}",
            f"来源：{self.source}",
        ]
        if self.transport == "stdio":
            lines.extend([
                f"Executable：{self.command}",
                "Arguments：" + json.dumps(self.args, ensure_ascii=False),
                f"Working directory：{self.cwd or '(Server 私有 runtime 目录)'}",
            ])
        else:
            lines.extend([
                f"URL：{self.url}",
                f"Timeout：{self.timeout_seconds}s",
            ])
        return "\n".join(lines)


def create_mcp_install_proposal_spec() -> ToolSpec:
    async def propose(
        input_data: McpInstallProposalInput,
    ) -> ApprovalRequest:
        return ApprovalRequest(
            id=f"approval-{uuid4().hex}",
            action=MCP_INSTALL_ACTION,
            payload=input_data.frozen_payload(),
            summary=input_data.approval_summary(),
            risk=(
                "批准后 Application 将持久保存并启动该外部 MCP Server；"
                "新能力仅在新 Session 生效。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_MCP_INSTALL,
        description=(
            "在用户要求安装 MCP Server 时，整理完整配置并提交待确认方案。"
            "信息不足时先在普通对话中询问；不得猜测路径、URL、Secret 或未经验证的包名。"
            "只接受单进程 stdio 启动配置或无 Secret 的 HTTP URL。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(McpInstallProposalInput),
        handler=propose,
        control_boundary=True,
    )


class McpInstallApprovalHandler:
    action = MCP_INSTALL_ACTION

    def __init__(self, service: McpApplicationService) -> None:
        self._service = service

    async def execute(
        self,
        payload: Mapping[str, Any],
    ) -> ApprovalExecution:
        data = dict(payload)
        record = await self._service.upsert_server(
            server_id=data["server_id"],
            display_name=data["display_name"],
            description=data["description"],
            transport=data["transport"],
            transport_config=data["transport_config"],
            enabled=False,
        )
        runtime = await self._service.test_server(record.id)
        if runtime.status is not RuntimeAvailability.AVAILABLE:
            return ApprovalExecution(
                succeeded=False,
                message=(
                    f"MCP Server `{record.id}` 已注册但连接测试失败，"
                    "配置保持 disabled。"
                ),
                data={
                    "server_id": record.id,
                    "enabled": False,
                    "revision": record.revision,
                    "runtime": runtime.to_dict(),
                },
            )
        enabled = await self._service.set_server_enabled(record.id, True)
        return ApprovalExecution(
            succeeded=True,
            message=(
                f"MCP Server `{enabled.id}` 安装、测试并启用成功；"
                "请新建 Session 使用该能力。"
            ),
            data={
                "server_id": enabled.id,
                "enabled": True,
                "revision": enabled.revision,
                "runtime": runtime.to_dict(),
            },
        )
