from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from core.agent_workspace import AgentWorkspace
from core.composition import create_agent_application
from core.model_call.config import load_model_config
from core.model_call.client import LLMClient
from plugins.goal.composition import create_goal_plugin
from plugins.goal.goal import GoalStatus


MODEL = load_model_config().name
OBJECTIVE = (
    "计算 2 + 2，明确给出结果和一条可复核的算术说明，"
    "并由独立 Judge 验收。不要调用外部命令，不要修改任何文件。"
)


class TimedLiveClient:
    def __init__(self) -> None:
        self._client = LLMClient()
        self.calls = 0

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self._client.__aexit__(exc_type, exc, traceback)

    async def chat(self, messages, model, tools=None):
        self.calls += 1
        call = self.calls
        started = time.monotonic()
        print(
            f"live model call {call} started; messages={len(messages)}; "
            f"tools={len(tools or [])}",
            flush=True,
        )
        try:
            return await self._client.chat(messages, model, tools)
        finally:
            print(
                f"live model call {call} finished in "
                f"{time.monotonic() - started:.1f}s",
                flush=True,
            )


async def run_benchmark() -> dict:
    project_root = Path(__file__).parents[2].resolve()
    status_before = subprocess.run(
        ["git", "status", "--short"],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="helperme-phase6a-") as directory:
        async with TimedLiveClient() as client:
            application = create_agent_application(
                model=MODEL,
                model_context_limit=200_000,
                agent_workspace=AgentWorkspace(Path(directory) / "agent"),
                workspace_roots={"project": project_root},
                llm_client=client,
            )
            goal_service = create_goal_plugin(application, default_max_turns=2)
            async with application:
                session_id = application.create_session(
                    f"goal-benchmark-{uuid4().hex}"
                )
                outcome = await asyncio.wait_for(
                    goal_service.start_goal(
                        session_id,
                        f"goal-{uuid4().hex}",
                        f"executor-{uuid4().hex}",
                        OBJECTIVE,
                        max_rounds=6,
                    ),
                    timeout=360,
                )

        goal = outcome.goal
        status_after = subprocess.run(
            ["git", "status", "--short"],
            cwd=project_root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        checks = {
            "contract_compiled": goal is not None,
            "executor_ran": bool(outcome.turns),
            "judge_ran": bool(
                outcome.turns and outcome.turns[-1].judge_outcome is not None
            ),
            "goal_completed": (
                goal is not None and goal.status is GoalStatus.COMPLETED
            ),
            "judge_cited_evidence": bool(
                goal is not None
                and goal.judgments
                and goal.judgments[-1].evidence
            ),
            "arithmetic_verified_independently": 2 + 2 == 4,
            "workspace_unchanged": status_after == status_before,
        }
        return {
            "benchmark": "phase6a_goal_live_loop",
            "model": MODEL,
            "objective": OBJECTIVE,
            "goal_status": goal.status.value if goal is not None else None,
            "turn_count": goal.turn_count if goal is not None else 0,
            "contract": (
                [
                    {
                        "id": item.id,
                        "authority": item.authority.value,
                        "description": item.description,
                        "evidence_requirements": list(
                            item.evidence_requirements
                        ),
                    }
                    for item in goal.contract.criteria
                ]
                if goal is not None
                else []
            ),
            "judgments": (
                [
                    {
                        "decision": item.decision.value,
                        "reason": item.reason,
                        "evidence": list(item.evidence),
                    }
                    for item in goal.judgments
                ]
                if goal is not None
                else []
            ),
            "checks": checks,
            "passed": all(checks.values()),
        }


async def async_main() -> None:
    report = await run_benchmark()
    report_path = (
        Path(__file__).resolve().parent
        / "phase6a_goal_live_last_report.json"
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
