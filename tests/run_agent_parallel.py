"""并发运行 core/agent.py，各 run 先写临时文件，全部结束后按序合并到当天日志。"""

import argparse
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PARALLEL_LOG_DIR = Path(__file__).resolve().parent / ".parallel_logs"
AGENT_MODULE = "core.agent"


def get_default_run_log_path() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return PROJECT_ROOT / f"run_{today}.log"


def run_one(run_id: int, log_dir: Path) -> tuple[int, int, str, str, Path]:
    log_file = log_dir / f"run_{run_id:03d}.log"
    env = os.environ.copy()
    env["HELPER_RUN_LOG_PATH"] = str(log_file)
    proc = subprocess.run(
        [sys.executable, "-m", AGENT_MODULE],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return run_id, proc.returncode, proc.stdout, proc.stderr, log_file


def merge_run_logs(log_files: list[Path], output_log: Path) -> None:
    with output_log.open("a", encoding="utf-8") as out:
        for log_file in log_files:
            if not log_file.exists():
                continue
            content = log_file.read_text(encoding="utf-8")
            out.write(content)
            if not content.endswith("\n"):
                out.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="并发运行 core/agent.py 若干次")
    parser.add_argument(
        "-n", "--count", type=int, default=2, help="并发次数，默认 2"
    )
    args = parser.parse_args()

    if args.count < 1:
        parser.error("count 至少为 1")

    if PARALLEL_LOG_DIR.exists():
        shutil.rmtree(PARALLEL_LOG_DIR)
    PARALLEL_LOG_DIR.mkdir(parents=True)
    run_log = get_default_run_log_path()

    batch_header = [
        "",
        "=" * 60,
        f"=== batch start @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | count={args.count} ===",
        "=" * 60,
    ]
    with run_log.open("a", encoding="utf-8") as f:
        f.write("\n".join(batch_header) + "\n")

    print(f"并发启动 {args.count} 个 agent ...")
    results: list[tuple[int, int, str, str, Path]] = []
    with ThreadPoolExecutor(max_workers=args.count) as pool:
        futures = [
            pool.submit(run_one, i + 1, PARALLEL_LOG_DIR) for i in range(args.count)
        ]
        for fut in as_completed(futures):
            run_id, code, out, err, log_file = fut.result()
            results.append((run_id, code, out, err, log_file))
            status = "OK" if code == 0 else f"FAIL({code})"
            print(f"  run #{run_id} 完成: {status}")

    results.sort(key=lambda x: x[0])
    merge_run_logs([log_file for _, _, _, _, log_file in results], run_log)

    summary_lines = [
        "",
        f"--- batch summary @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---",
    ]
    for run_id, code, out, err in [(r[0], r[1], r[2], r[3]) for r in results]:
        summary_lines.append(f"run #{run_id}: exit={code}")
        if out.strip():
            summary_lines.append(f"  stdout:\n{out.rstrip()}")
        if err.strip():
            summary_lines.append(f"  stderr:\n{err.rstrip()}")

    with run_log.open("a", encoding="utf-8") as f:
        f.write("\n".join(summary_lines) + "\n")

    shutil.rmtree(PARALLEL_LOG_DIR, ignore_errors=True)

    failed = sum(1 for _, code, _, _, _ in results if code != 0)
    print(f"全部完成：{args.count - failed}/{args.count} 成功，日志见 {run_log}")


if __name__ == "__main__":
    main()
