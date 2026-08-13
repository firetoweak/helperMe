from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from core.agent_workspace import AgentWorkspace
from core.approval import ApprovalActionRegistry
from core.composition import create_agent_application
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tools_runtime.run_invocation import RunInvocation
from tests.core.llm_test_support import call_result
from plugins.mcp.composition import create_mcp_plugin


class ScriptedClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    async def chat(self, messages, model, tools=None):
        self.requests.append({"messages": messages, "tools": tools or []})
        return call_result(self.responses.pop(0))


async def run_benchmark() -> dict:
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "mcp_stdio_server.py"
    ).resolve()
    proposal = {
        "server_id": "real_stdio",
        "display_name": "Real stdio",
        "description": "真实 stdio benchmark server",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(fixture)],
        "source": "user_input",
    }
    client = ScriptedClient([
        LLMResponse(calls=(ToolCall(
            "proposal-1",
            "propose_mcp_install",
            json.dumps(proposal, ensure_ascii=False),
        ),)),
        LLMResponse(content="当前 Session 仍保持原能力快照。"),
        LLMResponse(calls=(ToolCall(
            "load-1",
            "load_toolset",
            json.dumps({"toolset_id": "mcp:real_stdio"}),
        ),)),
        LLMResponse(calls=(ToolCall(
            "mcp-1",
            "mcp__real_stdio__read_test_token",
            "{}",
        ),)),
        LLMResponse(content="真实 MCP 工具调用完成。"),
    ])

    with TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = AgentWorkspace(root / "agent")
        task_workspace = root / "task"
        task_workspace.mkdir()
        plugin = create_mcp_plugin(workspace)
        actions = ApprovalActionRegistry()
        actions.register(plugin.install_approval_handler)
        application = create_agent_application(
            model="benchmark-model",
            model_context_limit=200_000,
            agent_workspace=workspace,
            workspace_roots={"project": task_workspace},
            llm_client=client,
            application_resources=(plugin.client_manager,),
            additional_tool_specs=(plugin.install_proposal_spec,),
            default_toolset_provider=plugin.toolset_provider,
            approval_actions=actions,
            runtime_mode=PlainMode(),
        )

        async with application:
            old_session_id = application.create_session("old-session")
            proposal_outcome = await application.start(
                old_session_id,
                "proposal-run",
                "请安装这个真实 stdio MCP Server",
                invocation=RunInvocation(
                    toolset_provider=plugin.toolset_provider,
                ),
            )
            pending = application.pending_approval(old_session_id)
            resolution = await application.resolve_approval(
                old_session_id,
                "yes",
            )
            old_outcome = await application.start(
                old_session_id,
                "old-session-run",
                "检查当前 Session 的能力",
                invocation=RunInvocation(
                    toolset_provider=plugin.toolset_provider,
                ),
            )
            new_session_id = application.create_session("new-session")
            new_outcome = await application.start(
                new_session_id,
                "new-session-run",
                "加载并调用刚安装的 MCP",
                invocation=RunInvocation(
                    toolset_provider=plugin.toolset_provider,
                ),
            )

            old_session = application._session_runtime.get_session(
                old_session_id
            )
            new_session = application._session_runtime.get_session(
                new_session_id
            )
            tool_results = [
                json.loads(message["content"])
                for message in new_session.conversation.protocol_messages()
                if message.get("role") == "tool"
            ]
            checks = {
                "proposal_blocked": (
                    proposal_outcome.result.status.value == "blocked"
                    and pending is not None
                ),
                "approved_install_succeeded": (
                    resolution.execution is not None
                    and resolution.execution.succeeded
                ),
                "old_session_snapshot_unchanged": (
                    old_session.capability_snapshot is not None
                    and old_session.capability_snapshot.toolsets == {}
                    and old_outcome.result.status.value == "completed"
                ),
                "new_session_captured_installed_revision": (
                    new_session.capability_snapshot is not None
                    and "mcp:real_stdio"
                    in new_session.capability_snapshot.toolsets
                ),
                "new_session_loaded_toolset": any(
                    item["code"] == "TOOLSET_LOADED"
                    for item in tool_results
                ),
                "real_mcp_tool_called": any(
                    item["code"] == "MCP_TOOL_OK"
                    for item in tool_results
                ),
                "new_run_completed": (
                    new_outcome.result.status.value == "completed"
                ),
            }
            return {
                "benchmark": "phase6b_conversation_mcp_install",
                "checks": checks,
                "passed": all(checks.values()),
                "installed_revision": (
                    resolution.execution.data.get("revision")
                    if resolution.execution is not None
                    else None
                ),
                "install_message": (
                    resolution.execution.message
                    if resolution.execution is not None
                    else None
                ),
                "install_data": (
                    dict(resolution.execution.data)
                    if resolution.execution is not None
                    else None
                ),
                "tool_result_codes": [
                    result["code"] for result in tool_results
                ],
            }


async def async_main() -> None:
    report = await run_benchmark()
    report_path = (
        Path(__file__).resolve().parent
        / "phase6b_mcp_install_last_report.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(async_main())
