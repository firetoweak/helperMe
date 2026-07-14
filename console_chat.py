from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

from core.agent import (
    DEFAULT_SYSTEM_PROMPT,
    FILE_RULE,
    Agent,
    _build_run_trace,
    _write_run_log,
    get_default_run_log_path,
)
from core.session_state import SessionStatus


TERMINAL_SESSION_STATUSES = {
    SessionStatus.BLOCKED,
    SessionStatus.FAILED,
}


def _create_agent(model: str) -> Agent:
    agent = Agent(model=model)
    agent.conversation.set_system_prompt(DEFAULT_SYSTEM_PROMPT + FILE_RULE)
    return agent


def _run_with_interrupt(agent: Agent, user_message: str) -> str:
    answers: list[str] = []
    errors: list[BaseException] = []
    finished = threading.Event()

    def run() -> None:
        try:
            answers.append(agent.run(user_message))
        except BaseException as exc:
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
            if agent.session.status != SessionStatus.RUNNING:
                continue

            agent.request_interrupt("console_interrupt")
            interrupt_requested = True
            print("\n已请求中断，正在等待 Agent 到达安全点……")

    worker.join()
    if errors:
        raise errors[0]
    if len(answers) != 1:
        raise RuntimeError("Agent run 未返回唯一结果")
    return answers[0]


def _format_session_flow(agent: Agent) -> str:
    return " -> ".join(event.kind.value for event in agent.session.events)


def main() -> None:
    model = os.environ.get("HELPER_MODEL", "qwen27b")
    log_path = (
        Path(os.environ["HELPER_RUN_LOG_PATH"])
        if "HELPER_RUN_LOG_PATH" in os.environ
        else get_default_run_log_path()
    )

    agent = _create_agent(model)

    print(f"Session 手动测试已启动。model={model}")
    print("输入任务开始；运行期间按 Ctrl+C 请求安全中断。")
    print("在输入提示处按 Ctrl+C 或 Ctrl+D 退出。")
    print(f"日志路径：{log_path}")

    while True:
        try:
            if agent.session.status == SessionStatus.INTERRUPTED:
                prompt = "\n你（继续）："
            elif agent.session.status == SessionStatus.PENDING:
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
        answer = _run_with_interrupt(agent, user_message)

        trace = _build_run_trace(
            started_at=started_at,
            question=user_message,
            answer=answer,
            agent=agent,
        )
        _write_run_log(trace, log_path)

        print(f"\n助手：{answer}")
        print(f"Session 状态：{agent.session.status.value}")
        print(f"Event 流：{_format_session_flow(agent)}")
        print(f"\n日志已写入：{log_path}")

        if agent.session.status in TERMINAL_SESSION_STATUSES:
            print("当前 Session 已结束；下一条输入将创建新的 Session。")
            agent = _create_agent(model)


if __name__ == "__main__":
    main()
