from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from core.agent_application import AgentApplication
from core.composition import create_agent_application
from core.observability import (
    build_run_trace,
    get_default_run_log_path,
    write_run_log,
)
from core.session_runner import SessionRunOutcome
from core.tools_runtime.run_runtime import RunStatus


TERMINAL_RUN_STATUSES = {
    RunStatus.BLOCKED,
    RunStatus.FAILED,
}

MODEL_CONTEXT_LIMIT = 70_768


def _new_session(application: AgentApplication) -> str:
    session_id = f"session-{uuid4().hex}"
    return application.create_session(session_id)


def _run_with_interrupt(
    application: AgentApplication,
    session_id: str,
    run_id: str,
    user_message: str,
    *,
    resume: bool,
) -> SessionRunOutcome:
    outcomes: list[SessionRunOutcome] = []
    errors: list[Exception] = []
    finished = threading.Event()

    def run() -> None:
        try:
            use_case = application.resume if resume else application.start
            outcomes.append(use_case(session_id, run_id, user_message))
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
            application.request_interrupt(session_id, "console_interrupt")
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


def main() -> None:
    model = os.environ.get("HELPER_MODEL", "qwen27b")

    application = create_agent_application(
        model,
        model_context_limit=MODEL_CONTEXT_LIMIT,
        runtime_root=Path.home() / ".helper-me" / "runtime",
    )
    session_id = _new_session(application)
    log_path = _resolve_log_path()
    last_status: RunStatus | None = None

    print(f"Session 手动测试已启动。model={model}")
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
        outcome = _run_with_interrupt(
            application,
            session_id,
            f"run-{uuid4().hex}",
            user_message,
            resume=last_status == RunStatus.INTERRUPTED,
        )
        last_status = outcome.result.status

        trace = build_run_trace(
            started_at=started_at,
            model=model,
            question=user_message,
            outcome=outcome,
        )
        write_run_log(trace, log_path)

        print(f"\n助手：{outcome.result.answer}")
        print(f"Run 状态：{last_status.value}")
        print(f"\n日志已写入：{log_path}")

        if last_status in TERMINAL_RUN_STATUSES:
            print("当前 Session 已结束；下一条输入将创建新的 Session。")
            session_id = _new_session(application)
            log_path = _resolve_log_path()
            last_status = None
            print(f"新 Session 日志路径：{log_path}")


if __name__ == "__main__":
    main()
