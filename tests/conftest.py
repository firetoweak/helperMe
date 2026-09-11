from __future__ import annotations

import os


collect_ignore = []
if os.environ.get("HELPERME_RUN_LIVE_TESTS") != "1":
    collect_ignore.append("live")


def pytest_addoption(parser):
    parser.addoption(
        "--all",
        action="store_true",
        default=False,
        help="也包括 process 层（真实进程、shell、MCP 传输）",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--all"):
        return
    if (config.option.markexpr or "").strip():
        return
    process_items = [item for item in items if item.get_closest_marker("process")]
    if not process_items or len(process_items) == len(items):
        return
    config.hook.pytest_deselected(items=process_items)
    items[:] = [item for item in items if item.get_closest_marker("process") is None]


def pytest_report_header(config):
    if config.getoption("--all"):
        layer = "all（default + process）"
    elif config.option.markexpr == "process":
        layer = "process"
    elif config.option.markexpr == "live":
        layer = "live"
    else:
        layer = "default（混合收集时不含 process）"
    return f"helperme tests: {layer}"
