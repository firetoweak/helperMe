from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from core.agent_workspace import AgentWorkspace
from core.agent_application import AgentApplication
from core.composition import create_agent_application
from core.model_call.client import LLMClient
from core.model_call.config import load_app_config
from core.observability import (
    build_turn_trace,
    get_default_turn_log_path,
    write_turn_log,
)
from core.session import SessionTurnOutcome
from core.tools_runtime.turn_runtime import TurnStatus
from plugins.goal.composition import create_goal_plugin
from plugins.goal.console import GoalCommandError, GoalConsoleAdapter
from plugins.mcp.composition import create_mcp_plugin
from plugins.mcp.console import McpCommandError, McpConsoleAdapter
from core.tools_runtime.turn_invocation import TurnInvocation
from core.environment import FilesystemAccessMode
from core.approval import ApprovalActionRegistry


TERMINAL_TURN_STATUSES = {
    TurnStatus.BLOCKED,
    TurnStatus.FAILED,
}

class ConsoleProgressSink:
    def emit(self, text: str) -> None:
        print(f"\n助手：{text}")


def _new_session(application: AgentApplication) -> str:
    session_id = f"session-{uuid4().hex}"
    return application.create_session(session_id)


def _handle_new_session_command(
    application: AgentApplication,
    user_message: str,
) -> str | None:
    if user_message != "/new":
        return None
    return _new_session(application)


def _resolve_log_path() -> Path:
    if "HELPER_TURN_LOG_PATH" in os.environ:
        return Path(os.environ["HELPER_TURN_LOG_PATH"])
    return get_default_turn_log_path()


def _latest_input_tokens(outcome: SessionTurnOutcome) -> int:
    usages = [
        checkpoint.data["input_tokens"]
        for checkpoint in outcome.result.checkpoints
        if checkpoint.reason == "llm_usage"
    ]
    return usages[-1] if usages else 0


def _format_token_limit(tokens: int) -> str:
    if tokens % 1_000 == 0:
        return f"{tokens // 1_000}K"
    return str(tokens)


async def async_main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    argparse.ArgumentParser(
        description="配置统一从 model_config.yaml 读取"
    ).parse_args(argv)
    app_config = load_app_config()
    model_config = app_config.model
    runtime_config = app_config.runtime
    model = model_config.name

    llm_client = LLMClient(model_config)
    agent_workspace = AgentWorkspace.default()
    mcp_plugin = create_mcp_plugin(agent_workspace)
    approval_actions = ApprovalActionRegistry()
    approval_actions.register(mcp_plugin.install_approval_handler)
    approval_actions.register(mcp_plugin.recovery_approval_handler)
    application = create_agent_application(
        model,
        model_context_limit=runtime_config.model_context_limit,
        agent_workspace=agent_workspace,
        workspace_roots={
            "project": app_config.workspace.root,
        },
        input_budget_ratio=runtime_config.input_budget_ratio,
        llm_client=llm_client,
        progress_sink=ConsoleProgressSink(),
        filesystem_access_mode=(
            FilesystemAccessMode.HOST
            if app_config.workspace.full_access
            else FilesystemAccessMode.SCOPED
        ),
        default_max_steps=runtime_config.max_steps,
        application_resources=(llm_client, mcp_plugin.client_manager),
        additional_tool_specs=(
            mcp_plugin.install_proposal_spec,
            *mcp_plugin.management_specs,
            mcp_plugin.recovery_proposal_spec,
        ),
        default_toolset_provider=mcp_plugin.toolset_provider,
        approval_actions=approval_actions,
    )
    async with application:
        session_id = _new_session(application)
        goal_console = GoalConsoleAdapter(
            create_goal_plugin(
                application,
                default_max_turns=runtime_config.max_goal_turns,
            )
        )
        mcp_console = McpConsoleAdapter(mcp_plugin.service)
        log_path = _resolve_log_path()
        last_status: TurnStatus | None = None

        print(f"Session 手动测试已启动。model={model}")
        print(
            "文件工具访问："
            + (
                "整台电脑"
                if app_config.workspace.full_access
                else "配置的 Workspace"
            )
        )
        print(f"单次 Turn 最大轮次：{runtime_config.max_steps}")
        print(f"单个 Goal 最大 Turn 数：{runtime_config.max_goal_turns}")
        print("输入任务开始；运行期间按 Ctrl+C 请求安全中断。")
        print("在输入提示处按 Ctrl+C 或 Ctrl+D 退出。")
        print("新建会话：输入 /new")
        print("MCP 管理：输入 /mcp help")
        print(f"日志路径：{log_path}")

        while True:
            if last_status == TurnStatus.INTERRUPTED:
                prompt = "\n你（继续）："
            elif last_status is None:
                prompt = "\n你（新 Session）："
            else:
                prompt = "\n你："
            try:
                user_message = (await asyncio.to_thread(input, prompt)).strip()
            except EOFError:
                print("\n已退出。")
                break

            if not user_message:
                continue

            new_session_id = _handle_new_session_command(
                application,
                user_message,
            )
            if new_session_id is not None:
                session_id = new_session_id
                log_path = _resolve_log_path()
                last_status = None
                print("\n新 Session 已创建。")
                print(f"日志路径：{log_path}")
                continue

            pending_approval = application.pending_approval(session_id)
            if pending_approval is not None:
                if user_message not in {"yes", "no"}:
                    print(
                        "\n当前操作正在等待审批。"
                        "请输入 yes 确认，输入 no 取消。"
                    )
                    continue
                resolution = await application.resolve_approval(
                    session_id,
                    user_message,
                )
                if resolution.decision == "rejected":
                    print("\nMCP 操作已取消。")
                    last_status = None
                    continue
                execution = resolution.execution
                print(f"\nMCP：\n{execution.message}")
                if execution.succeeded:
                    session_id = _new_session(application)
                    log_path = _resolve_log_path()
                    last_status = None
                    print("新 Session 已创建，最新 MCP 配置现已生效。")
                else:
                    last_status = None
                continue

            if user_message == "/mcp reload":
                session_id = _new_session(application)
                log_path = _resolve_log_path()
                last_status = None
                print("新 Session 已创建，并已捕获最新 MCP 能力快照。")
                continue

            try:
                mcp_reply = await mcp_console.execute_if_handled(user_message)
            except McpCommandError as exc:
                print(f"\nMCP 命令错误：{exc}")
                continue
            if mcp_reply is not None:
                print(f"\nMCP：\n{mcp_reply}")
                continue

            started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            turn_id = f"turn-{uuid4().hex}"

            goal_loop_outcome = None
            turn_invocation = TurnInvocation(
                toolset_provider=mcp_plugin.toolset_provider,
            )

            async def execute() -> SessionTurnOutcome:
                nonlocal goal_loop_outcome
                goal_loop_outcome = await goal_console.execute_if_handled(
                    session_id,
                    turn_id,
                    user_message,
                )
                if goal_loop_outcome is not None:
                    return goal_loop_outcome.final_session_outcome
                use_case = (
                    application.resume
                    if last_status == TurnStatus.INTERRUPTED
                    else application.start
                )
                return await use_case(
                    session_id,
                    turn_id,
                    user_message,
                    invocation=turn_invocation,
                )

            def request_interrupt() -> None:
                if not goal_console.request_pause(session_id):
                    application.request_interrupt(
                        session_id,
                        "console_interrupt",
                    )

            previous_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda *_: request_interrupt())
            try:
                outcome = await execute()
            except GoalCommandError as exc:
                print(f"\n命令错误：{exc}")
                continue
            finally:
                signal.signal(signal.SIGINT, previous_handler)
            last_status = outcome.result.status
            if (
                goal_loop_outcome is not None
                and goal_loop_outcome.goal is None
            ):
                # Contract 编译发生在隔离 Session；失败或中断不改变主 Session。
                last_status = None

            trace = build_turn_trace(
                started_at=started_at,
                model=model,
                question=user_message,
                outcome=outcome,
            )
            write_turn_log(trace, log_path)

            print(f"\n助手：{outcome.result.answer}")
            print(f"Turn 状态：{last_status.value}")
            print(
                "上下文 Token："
                f"{_latest_input_tokens(outcome)}/"
                f"{_format_token_limit(runtime_config.model_context_limit)}"
            )
            print(f"当前模型：{model}")
            print(f"\n日志已写入：{log_path}")

            if (
                last_status in TERMINAL_TURN_STATUSES
                and application.pending_approval(session_id) is None
            ):
                print("当前 Session 已结束；下一条输入将创建新的 Session。")
                session_id = _new_session(application)
                log_path = _resolve_log_path()
                last_status = None
                print(f"新 Session 日志路径：{log_path}")


def main(argv: list[str] | None = None) -> None:
    asyncio.run(async_main(argv))


if __name__ == "__main__":
    main()
