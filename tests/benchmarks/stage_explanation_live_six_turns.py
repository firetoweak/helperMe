import asyncio
from pathlib import Path
import sys
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.composition import create_agent_application
from core.model_call.client import LLMClient
from core.model_call.config import load_app_config
from core.tools_runtime.turn_runtime import TurnStatus


PROMPTS = (
    "当前项目是怎么设计的？",
    "我想知道当前agent调用command cli的详细设计。",
    "看上去还不错",
    "有啥能优化的呢？",
    "我当前电脑的配置是啥？",
    "可以的。",
)


class RecordingConsoleProgressSink:
    def __init__(self) -> None:
        self.current_turn = 0
        self.records: list[tuple[int, str]] = []

    def emit(self, text: str) -> None:
        self.records.append((self.current_turn, text))
        print(f"\n阶段性说明：{text}")


async def async_main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    app_config = load_app_config()
    sink = RecordingConsoleProgressSink()
    llm_client = LLMClient(app_config.model)
    application = create_agent_application(
        app_config.model.name,
        model_context_limit=200_000,
        agent_workspace=AgentWorkspace.default(),
        workspace_roots={"project": app_config.workspace_root},
        input_budget_ratio=0.9,
        llm_client=llm_client,
        progress_sink=sink,
        application_resources=(llm_client,),
    )
    async with application:
        session_id = application.create_session(f"stage-test-{uuid4().hex}")

        for turn, prompt in enumerate(PROMPTS, start=1):
            sink.current_turn = turn
            print(f"\n\n=== Turn {turn} ===\n用户：{prompt}")
            outcome = await application.start(
                session_id,
                f"turn-{uuid4().hex}",
                prompt,
            )
            print(f"\n最终回答：{outcome.result.answer}")
            print(f"Turn 状态：{outcome.result.status.value}")
            if outcome.result.status != TurnStatus.COMPLETED:
                raise RuntimeError(
                    f"Turn {turn} 未完成：{outcome.result.status.value}"
                )

    print("\n\n=== 阶段性说明统计 ===")
    for turn in range(1, len(PROMPTS) + 1):
        count = sum(record_turn == turn for record_turn, _ in sink.records)
        print(f"Turn {turn}: {count}")


if __name__ == "__main__":
    asyncio.run(async_main())
from core.agent_workspace import AgentWorkspace
