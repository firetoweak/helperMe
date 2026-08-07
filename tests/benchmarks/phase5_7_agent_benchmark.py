from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from core.composition import create_agent_application
from core.observability import build_run_trace


MODEL = "qwen27b"
USER_GOAL = (
    "这是一个刚检出的项目，目前不能正常交付。请自行检查项目并修复问题，"
    "使它的测试和构建全部通过。完成后请依据实际验证结果和 Git 改动准确总结。"
)


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
        "package.json": """{
  "name": "phase5-command-benchmark",
  "version": "1.0.0",
  "type": "module",
  "private": true,
  "dependencies": {
    "tiny-invariant": "file:vendor/tiny-invariant"
  },
  "scripts": {
    "test": "node --test",
    "build": "node scripts/build.js"
  }
}
""",
        "README.md": """# Discount calculator

会员享受九折优惠，非会员不打折。金额必须是非负数。
""",
        "src/discount.js": """import invariant from "tiny-invariant";

export function calculateTotal(subtotal, isMember) {
  invariant(subtotal >= 0, "subtotal must be non-negative");
  const discountRate = isMember ? 0.2 : 0;
  return Number((subtotal * (1 - discountRate)).toFixed(2));
}
""",
        "test/discount.test.js": """import test from "node:test";
import assert from "node:assert/strict";
import { calculateTotal } from "../src/discount.js";

test("members receive a ten percent discount", () => {
  assert.equal(calculateTotal(100, true), 90);
});

test("non-members pay the full amount", () => {
  assert.equal(calculateTotal(100, false), 100);
});

test("negative subtotals are rejected", () => {
  assert.throws(() => calculateTotal(-1, true), RangeError);
});
""",
        "vendor/tiny-invariant/package.json": """{
  "name": "tiny-invariant",
  "version": "1.0.0",
  "type": "module",
  "exports": "./index.js"
}
""",
        "vendor/tiny-invariant/index.js": """export default function invariant(condition, message) {
  if (!condition) {
    throw new RangeError(message);
  }
}
""",
        "scripts/build.js": """import { mkdir, readFile, writeFile } from "node:fs/promises";

const source = await readFile(new URL("../src/discount.js", import.meta.url), "utf8");
if (!source.includes("calculateTotal")) {
  throw new Error("missing calculateTotal export");
}
await mkdir(new URL("../dist/", import.meta.url), { recursive: true });
await writeFile(new URL("../dist/discount.js", import.meta.url), source);
console.log("build completed");
""",
        ".gitignore": "node_modules/\ndist/\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


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


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    benchmark_root = Path(tempfile.mkdtemp(prefix="helperme-phase5-7-"))
    project_root = benchmark_root / "project"
    runtime_root = benchmark_root / "runtime"
    report_path = Path(__file__).resolve().parent / "phase5_7_last_report.json"
    project_root.mkdir()
    write_fixture(project_root)

    fixture_install = run(["npm.cmd", "install"], project_root)
    if fixture_install.returncode != 0:
        raise RuntimeError(fixture_install.stderr)
    shutil.rmtree(project_root / "node_modules")

    run(["git", "init"], project_root)
    run(["git", "config", "user.email", "benchmark@example.com"], project_root)
    run(["git", "config", "user.name", "Phase 5 Benchmark"], project_root)
    run(["git", "add", "."], project_root)
    commit = run(["git", "commit", "-m", "initial failing fixture"], project_root)
    if commit.returncode != 0:
        raise RuntimeError(commit.stderr)

    initial_test = run(["npm.cmd", "test"], project_root)
    if initial_test.returncode == 0:
        raise RuntimeError("benchmark fixture 必须以失败测试开始")

    application = create_agent_application(
        model=MODEL,
        model_context_limit=200_000,
        runtime_root=runtime_root,
        workspace_roots={"project": project_root},
        input_budget_ratio=0.9,
    )
    session_id = application.create_session(f"benchmark-{uuid4().hex}")
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    outcome = application.start(
        session_id,
        f"run-{uuid4().hex}",
        USER_GOAL,
        max_rounds=50,
    )

    session = application._session_runtime.sessions[session_id]
    messages = session.conversation.protocol_messages()
    timeline = tool_timeline(messages)
    final_test = run(["npm.cmd", "test"], project_root)
    final_build = run(["npm.cmd", "run", "build"], project_root)
    diff = run(["git", "diff", "--", "."], project_root)
    status = run(["git", "status", "--short"], project_root)

    commands = [
        step["arguments"].get("command", "")
        for step in timeline
        if step["name"] == "execute_command"
    ]
    write_indexes = [
        index
        for index, step in enumerate(timeline)
        if step["name"] in {"apply_patch", "replace_all", "write_file"}
    ]
    first_write = min(write_indexes, default=len(timeline))
    tests_before_write = any(
        index < first_write and "test" in step["arguments"].get("command", "").lower()
        for index, step in enumerate(timeline)
        if step["name"] == "execute_command"
    )
    tests_after_write = any(
        index > first_write and "test" in step["arguments"].get("command", "").lower()
        for index, step in enumerate(timeline)
        if step["name"] == "execute_command"
    )
    checks = {
        "agent_completed": outcome.result.status.value == "completed",
        "discovered_project": any(
            step["name"] in {"get_workspace_info", "glob", "read_file", "grep"}
            for step in timeline
        ),
        "installed_dependencies": any(
            "npm install" in command.lower() or "npm ci" in command.lower()
            for command in commands
        ),
        "observed_failing_test_before_edit": tests_before_write,
        "modified_code": bool(write_indexes),
        "retested_after_edit": tests_after_write,
        "ran_build": any("build" in command.lower() for command in commands),
        "called_get_changes": any(step["name"] == "get_changes" for step in timeline),
        "independent_test_passed": final_test.returncode == 0,
        "independent_build_passed": final_build.returncode == 0,
        "git_diff_contains_fix": "discountRate = isMember ? 0.1 : 0" in diff.stdout,
        "workspace_changes_match_claim": (
            status.stdout.strip() == "M src/discount.js"
            and "src/discount.js" in (outcome.result.answer or "")
        ),
        "final_answer_mentions_verified_results": (
            "测试" in (outcome.result.answer or "")
            and "构建" in (outcome.result.answer or "")
        ),
    }
    report = {
        "benchmark_root": str(benchmark_root),
        "model": MODEL,
        "goal": USER_GOAL,
        "initial_test_exit_code": initial_test.returncode,
        "run_status": outcome.result.status.value,
        "final_reason": outcome.result.final_reason,
        "answer": outcome.result.answer,
        "tool_timeline": timeline,
        "independent_test": {
            "exit_code": final_test.returncode,
            "stdout": final_test.stdout,
            "stderr": final_test.stderr,
        },
        "independent_build": {
            "exit_code": final_build.returncode,
            "stdout": final_build.stdout,
            "stderr": final_build.stderr,
        },
        "git_diff": diff.stdout,
        "git_status": status.stdout,
        "checks": checks,
        "passed": all(checks.values()),
        "trace": build_run_trace(
            started_at=started_at,
            model=MODEL,
            question=USER_GOAL,
            outcome=outcome,
        ),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "report_path": str(report_path),
        "benchmark_root": str(benchmark_root),
        "run_status": report["run_status"],
        "checks": checks,
        "passed": report["passed"],
        "answer": report["answer"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
