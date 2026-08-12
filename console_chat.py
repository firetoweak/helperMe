from __future__ import annotations

import argparse
import os
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from core.agent_workspace import AgentWorkspace
from core.agent_application import AgentApplication
from core.composition import create_agent_application
from core.model_call.client import LLMClient
from core.model_call.config import load_app_config
from core.observability import (
    build_run_trace,
    get_default_run_log_path,
    write_run_log,
)
from core.session import SessionRunOutcome
from core.tools_runtime.run_runtime import RunStatus
from plugins.goal.composition import create_goal_plugin
from plugins.goal.console import GoalCommandError, GoalConsoleAdapter
from tools.workspace import FilesystemAccessMode


TERMINAL_RUN_STATUSES = {
    RunStatus.BLOCKED,
    RunStatus.FAILED,
}

class ConsoleProgressSink:
    def emit(self, text: str) -> None:
        print(f"\n助手：{text}")


def _new_session(application: AgentApplication) -> str:
    session_id = f"session-{uuid4().hex}"
    return application.create_session(session_id)


def _run_with_interrupt(
    execute: Callable[[], SessionRunOutcome],
    request_interrupt: Callable[[], None],
) -> SessionRunOutcome:
    outcomes: list[SessionRunOutcome] = []
    errors: list[Exception] = []
    finished = threading.Event()

    def run() -> None:
        try:
            outcomes.append(execute())
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=run, name="agent-run")
    worker.start()

    interrupt_requested = False
    while not finished.is_set():
        try:
            finished.wait(timeout=0.1)
        except KeyboardInterrupt:
            if interrupt_requested:
                print("\n中断请求已发送，正在等待安全点……")
                continue
            request_interrupt()
            interrupt_requested = True
            print("\n已请求中断，正在等待 Agent 到达安全点……")

    worker.join()
    if errors:
        raise errors[0]
    if len(outcomes) != 1:
        raise RuntimeError("AgentApplication 未返回唯一 SessionRunOutcome")
    return outcomes[0]


def _resolve_log_path() -> Path:
    if "HELPER_RUN_LOG_PATH" in os.environ:
        return Path(os.environ["HELPER_RUN_LOG_PATH"])
    return get_default_run_log_path()


def _latest_input_tokens(outcome: SessionRunOutcome) -> int:
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


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    argparse.ArgumentParser(
        description="配置统一从 model_config.yaml 读取"
    ).parse_args(argv)
    app_config = load_app_config()
    model_config = app_config.model
    runtime_config = app_config.runtime
    model = model_config.name

    application = create_agent_application(
        model,
        model_context_limit=runtime_config.model_context_limit,
        agent_workspace=AgentWorkspace.default(),
        workspace_roots={
            "project": app_config.workspace.root,
        },
        input_budget_ratio=runtime_config.input_budget_ratio,
        llm_client=LLMClient(model_config),
        progress_sink=ConsoleProgressSink(),
        filesystem_access_mode=(
            FilesystemAccessMode.HOST
            if app_config.workspace.full_access
            else FilesystemAccessMode.SCOPED
        ),
        default_max_rounds=runtime_config.max_rounds,
    )
    session_id = _new_session(application)
    goal_console = GoalConsoleAdapter(
        create_goal_plugin(
            application,
            default_max_turns=runtime_config.max_goal_turns,
        )
    )
    log_path = _resolve_log_path()
    last_status: RunStatus | None = None

    print(f"Session 手动测试已启动。model={model}")
    print(
        "文件工具访问："
        + (
            "整台电脑"
            if app_config.workspace.full_access
            else "配置的 Workspace"
        )
    )
    print(f"单次 Run 最大轮次：{runtime_config.max_rounds}")
    print(f"单个 Goal 最大 Turn 数：{runtime_config.max_goal_turns}")
    print("输入任务开始；运行期间按 Ctrl+C 请求安全中断。")
    print("在输入提示处按 Ctrl+C 或 Ctrl+D 退出。")
    print(f"日志路径：{log_path}")

    while True:
        try:
            if last_status == RunStatus.INTERRUPTED:
                prompt = "\n你（继续）："
            elif last_status is None:
                prompt = "\n你（新 Session）："
            else:
                prompt = "\n你："
            user_message = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            break

        if not user_message:
            continue

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run_id = f"run-{uuid4().hex}"

        goal_loop_outcome = None

        def execute() -> SessionRunOutcome:
            nonlocal goal_loop_outcome
            goal_loop_outcome = goal_console.execute_if_handled(
                session_id,
                run_id,
                user_message,
            )
            if goal_loop_outcome is not None:
                return goal_loop_outcome.final_session_outcome
            use_case = (
                application.resume
                if last_status == RunStatus.INTERRUPTED
                else application.start
            )
            return use_case(session_id, run_id, user_message)

        try:
            def request_interrupt() -> None:
                if not goal_console.request_pause(session_id):
                    application.request_interrupt(
                        session_id,
                        "console_interrupt",
                    )

            outcome = _run_with_interrupt(execute, request_interrupt)
        except GoalCommandError as exc:
            print(f"\n命令错误：{exc}")
            continue
        last_status = outcome.result.status
        if (
            goal_loop_outcome is not None
            and goal_loop_outcome.goal is None
        ):
            # Contract 编译发生在隔离 Session；失败或中断不改变主 Session。
            last_status = None

        trace = build_run_trace(
            started_at=started_at,
            model=model,
            question=user_message,
            outcome=outcome,
        )
        write_run_log(trace, log_path)

        print(f"\n助手：{outcome.result.answer}")
        print(f"Run 状态：{last_status.value}")
        print(
            "上下文 Token："
            f"{_latest_input_tokens(outcome)}/"
            f"{_format_token_limit(runtime_config.model_context_limit)}"
        )
        print(f"当前模型：{model}")
        print(f"\n日志已写入：{log_path}")

        if last_status in TERMINAL_RUN_STATUSES:
            print("当前 Session 已结束；下一条输入将创建新的 Session。")
            session_id = _new_session(application)
            log_path = _resolve_log_path()
            last_status = None
            print(f"新 Session 日志路径：{log_path}")


if __name__ == "__main__":
    main()
