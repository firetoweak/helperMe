from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.composition import create_agent_application
from core.goals import (
    CommandRequirement,
    GoalStatus,
    OutcomeDecision,
    TaskDraft,
    TaskStatus,
    TaskVerification,
    WorkspaceRequirement,
)
from core.model_call.config import load_model_config


MODEL = load_model_config().name
OBJECTIVE = "检查项目中的两个问题，分别修复并完成测试。"
RUN_MESSAGE = "执行当前 Goal Task。严格按当前 Task 的范围和验收标准行动，并提交明确结果。"


class BenchmarkProgressSink:
    def emit(self, text: str) -> None:
        print(f"[agent] {text}", flush=True)


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
        check=False,
    )


def write_fixture(root: Path) -> None:
    files = {
        "calculator.py": """def member_total(subtotal: float) -> float:
    return round(subtotal * 0.8, 2)
""",
        "test_calculator.py": """import unittest

from calculator import member_total


class CalculatorTest(unittest.TestCase):
    def test_member_receives_ten_percent_discount(self):
        self.assertEqual(member_total(100), 90)


if __name__ == "__main__":
    unittest.main()
""",
        "README.md": """# Calculator

会员享受九折优惠。

验证命令：

- 测试：`python -m unittest -v`
- 构建：`python scripts/build.py`
""",
        ".gitignore": "__pycache__/\n*.pyc\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *arguments], root)


def tool_timeline(messages: list[dict]) -> list[dict]:
    results: dict[str, dict] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            results[message["tool_call_id"]] = json.loads(message["content"])
        except json.JSONDecodeError:
            results[message["tool_call_id"]] = {"raw": message["content"]}

    timeline = []
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call["function"]
            result = results.get(call["id"], {})
            timeline.append({
                "name": function["name"],
                "arguments": json.loads(function["arguments"]),
                "result_code": result.get("code"),
                "result_ok": result.get("ok"),
            })
    return timeline


def command_seen(timeline: list[dict], fragment: str) -> bool:
    return any(
        step["name"] == "execute_command"
        and fragment in step["arguments"].get("command", "").lower()
        for step in timeline
    )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    benchmark_root = Path(tempfile.mkdtemp(prefix="helperme-phase6a-"))
    project_root = benchmark_root / "project"
    runtime_root = benchmark_root / "runtime"
    report_path = Path(__file__).resolve().parent / "phase6a_last_report.json"
    progress_path = Path(__file__).resolve().parent / "phase6a_progress.json"
    progress_path.unlink(missing_ok=True)
    project_root.mkdir()
    write_fixture(project_root)

    git(project_root, "init")
    git(project_root, "config", "user.email", "benchmark@example.com")
    git(project_root, "config", "user.name", "Phase 6A Benchmark")
    git(project_root, "add", ".")
    commit = git(project_root, "commit", "-m", "initial two-problem fixture")
    if commit.returncode != 0:
        raise RuntimeError(commit.stderr)
    initial_head = git(project_root, "rev-parse", "HEAD").stdout.strip()

    initial_test = run([sys.executable, "-m", "unittest", "-v"], project_root)
    initial_build = run([sys.executable, "scripts/build.py"], project_root)
    if initial_test.returncode == 0 or initial_build.returncode == 0:
        raise RuntimeError("benchmark fixture 必须同时以失败测试和失败构建开始")

    application = create_agent_application(
        model=MODEL,
        model_context_limit=200_000,
        runtime_root=runtime_root,
        workspace_roots={"project": project_root},
        input_budget_ratio=0.9,
        progress_sink=BenchmarkProgressSink(),
    )
    session_id = application.create_session(f"benchmark-{uuid4().hex}")
    goal_id = f"goal-{uuid4().hex}"
    service = application.goals
    goal = service.create_goal(
        session_id,
        goal_id,
        OBJECTIVE,
        [
            TaskDraft(
                "A",
                "定位两个问题；本 Task 只调查，不修改文件。",
                acceptance_criteria=(
                    "实际运行 README 中的测试与构建命令；准确指出会员折扣实现错误，"
                    "以及构建脚本 scripts/build.py 不存在；仓库必须保持无改动。"
                ),
                verification=TaskVerification(
                    commands=(
                        CommandRequirement(
                            "python -m unittest",
                            "project",
                            ".",
                            expected_exit_codes=None,
                        ),
                        CommandRequirement(
                            "scripts/build.py",
                            "project",
                            ".",
                            expected_exit_codes=None,
                        ),
                    ),
                    workspace=WorkspaceRequirement("project", changed=False),
                ),
            ),
            TaskDraft(
                "B",
                "修复问题一：会员折扣实现错误。",
                depends_on=("A",),
                acceptance_criteria=(
                    "只修改 calculator.py；python -m unittest -v 通过；"
                    "构建问题留给后续 Task。"
                ),
                verification=TaskVerification(
                    commands=(
                        CommandRequirement("python -m unittest", "project", "."),
                    ),
                    workspace=WorkspaceRequirement(
                        "project",
                        changed=True,
                        allowed_paths=("calculator.py",),
                    ),
                ),
            ),
            TaskDraft(
                "C",
                "修复问题二：编辑现有 scripts/build.py，使构建通过。",
                depends_on=("A",),
                acceptance_criteria=(
                    "只能编辑已经存在的 scripts/build.py；不得新增文件、改名或修改构建命令。"
                    "若前置调查证明该约束无法满足，必须请求重规划，不得越过验收标准。"
                ),
            ),
            TaskDraft(
                "D",
                "整体验证。",
                depends_on=("B", "C"),
                acceptance_criteria="测试与构建均通过，且改动与两个问题一致。",
                verification=TaskVerification(
                    commands=(
                        CommandRequirement("python -m unittest", "project", "."),
                        CommandRequirement("scripts/build.py", "project", "."),
                    ),
                    workspace=WorkspaceRequirement(
                        "project",
                        changed=True,
                        allowed_paths=("calculator.py", "scripts/build.py"),
                    ),
                ),
            ),
        ],
    )

    started_at = datetime.now().isoformat(timespec="seconds")
    session = application._session_runtime.sessions[session_id]
    run_reports = []
    statuses_after_runs = []

    def persist_progress() -> None:
        progress_path.write_text(
            json.dumps(
                {
                    "benchmark_root": str(benchmark_root),
                    "goal_id": goal.id,
                    "goal_status": goal.status.value,
                    "runs": run_reports,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def execute_task_run(index: int) -> None:
        print(
            f"[benchmark] start run {index}: task {goal.next_task().id}",
            flush=True,
        )
        before = len(session.conversation.protocol_messages())
        outcome = service.execute_next_task(
            session_id,
            goal_id,
            f"run-{index}-{uuid4().hex}",
            RUN_MESSAGE,
            max_rounds=20,
        )
        messages = session.conversation.protocol_messages()[before:]
        timeline = tool_timeline(messages)
        status = git(
            project_root, "status", "--short", "--untracked-files=all"
        ).stdout.strip()
        statuses_after_runs.append(status)
        run_reports.append({
            "run": index,
            "kind": "task",
            "task_id": outcome.task_id,
            "run_id": outcome.session_outcome.record.run_id,
            "run_status": outcome.session_outcome.result.status.value,
            "answer": outcome.session_outcome.result.answer,
            "outcome": (
                {
                    "decision": outcome.applied_outcome.decision.value,
                    "summary": outcome.applied_outcome.summary,
                    "evidence": list(outcome.applied_outcome.evidence),
                }
                if outcome.applied_outcome is not None
                else None
            ),
            "completion_review": (
                {
                    "accepted": outcome.completion_review.accepted,
                    "reason": outcome.completion_review.reason,
                }
                if outcome.completion_review is not None
                else None
            ),
            "tool_timeline": timeline,
            "git_status_after_run": status,
        })
        persist_progress()
        print(
            f"[benchmark] finish run {index}: task={outcome.task_id} "
            f"status={outcome.session_outcome.result.status.value} "
            f"applied={outcome.applied_outcome is not None} "
            f"goal={goal.status.value}",
            flush=True,
        )

    def execute_plan_run(index: int) -> None:
        print(f"[benchmark] start run {index}: plan revision", flush=True)
        before = len(session.conversation.protocol_messages())
        outcome = service.execute_plan_revision(
            session_id,
            goal_id,
            f"run-{index}-{uuid4().hex}",
            "根据当前失败事实重规划 Task；替代 Task 必须携带可执行 verification contract。",
            max_rounds=20,
        )
        messages = session.conversation.protocol_messages()[before:]
        timeline = tool_timeline(messages)
        status = git(
            project_root, "status", "--short", "--untracked-files=all"
        ).stdout.strip()
        statuses_after_runs.append(status)
        revision = outcome.applied_revision
        run_reports.append({
            "run": index,
            "kind": "plan_revision",
            "task_id": outcome.task_id,
            "run_id": outcome.session_outcome.record.run_id,
            "run_status": outcome.session_outcome.result.status.value,
            "answer": outcome.session_outcome.result.answer,
            "revision": (
                {
                    "reason": revision.reason,
                    "replacement_tasks": [
                        {
                            "id": task.id,
                            "description": task.description,
                            "depends_on": list(task.depends_on),
                            "acceptance_criteria": task.acceptance_criteria,
                            "has_verification": task.verification is not None,
                        }
                        for task in revision.replacement_tasks
                    ],
                    "dependency_changes": [
                        {
                            "task_id": change.task_id,
                            "depends_on": list(change.depends_on),
                        }
                        for change in revision.dependency_changes
                    ],
                }
                if revision is not None
                else None
            ),
            "tool_timeline": timeline,
            "git_status_after_run": status,
        })
        persist_progress()
        print(
            f"[benchmark] finish run {index}: plan revision "
            f"applied={outcome.applied_revision is not None} "
            f"goal={goal.status.value}",
            flush=True,
        )

    for index in range(1, 4):
        execute_task_run(index)

    next_index = 4
    while goal.status is not GoalStatus.COMPLETED and next_index <= 10:
        if goal.status is GoalStatus.REPLAN_REQUIRED:
            execute_plan_run(next_index)
        else:
            execute_task_run(next_index)
        next_index += 1

    final_test = run([sys.executable, "-m", "unittest", "-v"], project_root)
    final_build = run([sys.executable, "scripts/build.py"], project_root)
    final_diff = git(project_root, "diff", "--", ".").stdout
    decisions = [item.decision for item in goal.outcomes]
    task_reports = [item for item in run_reports if item["kind"] == "task"]
    plan_reports = [
        item for item in run_reports if item["kind"] == "plan_revision"
    ]
    timelines = [item["tool_timeline"] for item in task_reports]
    final_status_paths = {
        line[3:].strip()
        for line in git(
            project_root, "status", "--short", "--untracked-files=all"
        ).stdout.splitlines()
        if line.strip()
    }

    checks = {
        "all_runs_completed": all(
            item["run_status"] == "completed" for item in run_reports
        ),
        "first_three_runs_bound_to_expected_tasks": (
            [item["task_id"] for item in task_reports[:3]] == ["A", "B", "C"]
            and [link.task_id for link in goal.run_links[:3]] == ["A", "B", "C"]
            and len({link.run_id for link in goal.run_links}) == len(goal.run_links)
        ),
        "each_task_run_applied_one_outcome": (
            len(goal.outcomes) == len(task_reports)
            and all(item["outcome"] is not None for item in task_reports)
        ),
        "all_outcomes_have_evidence": all(item.evidence for item in goal.outcomes),
        "completed_outcomes_passed_gate": all(
            item["completion_review"] is None
            or item["completion_review"]["accepted"]
            for item in task_reports
        ),
        "run1_executed_both_verification_commands": (
            command_seen(timelines[0], "unittest")
            and command_seen(timelines[0], "scripts/build.py")
        ),
        "run1_did_not_modify_workspace": statuses_after_runs[0] == "",
        "run2_completed_after_acceptance_check": (
            decisions[1] is OutcomeDecision.COMPLETED
            and command_seen(timelines[1], "unittest")
        ),
        "run2_only_fixed_issue_one": statuses_after_runs[1] == "M calculator.py",
        "run3_requested_replan": decisions[2] is OutcomeDecision.REPLAN,
        "run3_respected_impossible_boundary": statuses_after_runs[2] == "M calculator.py",
        "plan_revision_applied": any(
            item["kind"] == "plan_revision" and item["revision"] is not None
            for item in run_reports
        ),
        "plan_revision_only_planned": all(
            all(
                step["name"] in {"submit_plan_revision", "rewrite_todos"}
                or not step["result_ok"]
                for step in item["tool_timeline"]
            )
            for item in plan_reports
        ),
        "original_impossible_task_superseded": (
            goal.task("C").status is TaskStatus.SUPERSEDED
        ),
        "goal_completed": (
            goal.status is GoalStatus.COMPLETED
            and goal.task("A").status is TaskStatus.COMPLETED
            and goal.task("B").status is TaskStatus.COMPLETED
            and goal.task("D").status is TaskStatus.COMPLETED
        ),
        "independent_test_passed": final_test.returncode == 0,
        "independent_build_passed": final_build.returncode == 0,
        "diff_contains_both_fixes": (
            "return round(subtotal * 0.9, 2)" in final_diff
            and "scripts/build.py" in final_status_paths
        ),
        "final_changes_stay_in_scope": (
            final_status_paths <= {"calculator.py", "scripts/build.py"}
        ),
        "git_history_unchanged": (
            git(project_root, "rev-parse", "HEAD").stdout.strip() == initial_head
        ),
    }
    report = {
        "started_at": started_at,
        "benchmark_root": str(benchmark_root),
        "model": MODEL,
        "goal_id": goal_id,
        "objective": OBJECTIVE,
        "initial_test_exit_code": initial_test.returncode,
        "initial_build_exit_code": initial_build.returncode,
        "runs": run_reports,
        "goal": {
            "status": goal.status.value,
            "replan_task_id": (
                goal.replan_task_id
                if goal.status is GoalStatus.REPLAN_REQUIRED
                else None
            ),
            "tasks": [
                {"id": task.id, "status": task.status.value}
                for task in goal.tasks
            ],
            "outcome_decisions": [decision.value for decision in decisions],
        },
        "final_test": {
            "exit_code": final_test.returncode,
            "stdout": final_test.stdout,
            "stderr": final_test.stderr,
        },
        "final_build": {
            "exit_code": final_build.returncode,
            "stdout": final_build.stdout,
            "stderr": final_build.stderr,
        },
        "git_diff": final_diff,
        "checks": checks,
        "passed": all(checks.values()),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "report_path": str(report_path),
        "benchmark_root": str(benchmark_root),
        "model": MODEL,
        "goal_status": goal.status.value,
        "decisions": [decision.value for decision in decisions],
        "checks": checks,
        "passed": report["passed"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
