from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CaptureLimit:
    max_chars: int = 6_000
    head_chars: int = 2_400

    def __post_init__(self) -> None:
        if type(self.max_chars) is not int or self.max_chars <= 0:
            raise ValueError("max_chars 必须大于 0")
        if (
            type(self.head_chars) is not int
            or not 0 <= self.head_chars <= self.max_chars
        ):
            raise ValueError("head_chars 必须位于 0 到 max_chars 之间")


@dataclass(frozen=True)
class CapturedOutput:
    content: str
    total_chars: int
    truncated: bool
    omitted_chars: int

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "content": self.content,
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "omitted_chars": self.omitted_chars,
        }


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    stdout: CapturedOutput
    stderr: CapturedOutput
    duration_ms: int
    timed_out: bool


class ShellNotFoundError(FileNotFoundError):
    def __init__(self, shell_name: str, executable: str) -> None:
        self.shell_name = shell_name
        self.executable = executable
        super().__init__(
            f"未找到 Shell 可执行程序: {shell_name} ({executable})"
        )


class CommandStartError(OSError):
    pass


class EnvironmentCommandExecutor(Protocol):
    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int,
    ) -> CommandResult:
        ...
