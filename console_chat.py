from __future__ import annotations

import os
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


def main() -> None:
    model = os.environ.get("HELPER_MODEL", "qwen27b")
    log_path = (
        Path(os.environ["HELPER_RUN_LOG_PATH"])
        if "HELPER_RUN_LOG_PATH" in os.environ
        else get_default_run_log_path()
    )

    agent = Agent(model=model)
    agent.conversation.set_system_prompt(DEFAULT_SYSTEM_PROMPT + FILE_RULE)

    print(f"多轮对话已启动。model={model}")
    print("按 Ctrl+C 或 Ctrl+D 退出。")
    print(f"日志路径：{log_path}")

    while True:
        try:
            user_message = input("\n你：").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            break

        if not user_message:
            continue

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        answer = agent.run(user_message)

        trace = _build_run_trace(
            started_at=started_at,
            question=user_message,
            answer=answer,
            agent=agent,
        )
        _write_run_log(trace, log_path)

        print(f"\n助手：{answer}")
        print(f"\n日志已写入：{log_path}")


if __name__ == "__main__":
    main()
