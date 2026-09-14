import os
from pathlib import Path


def write_marker(path: str) -> None:
    Path(path).write_text("ok\n", encoding="utf-8")


def write_path(path: str) -> None:
    Path(path).write_text(os.environ.get("PATH", ""), encoding="utf-8")
