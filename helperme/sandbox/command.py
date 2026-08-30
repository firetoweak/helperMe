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


class BoundedTextCapture:
    """Incrementally retain bounded head/tail text for command output."""

    def __init__(self, limit: CaptureLimit) -> None:
        self._limit = limit
        self._head = ""
        self._tail = ""
        self._total_chars = 0

    def feed(self, text: str) -> None:
        self._total_chars += len(text)
        remaining_head = self._limit.head_chars - len(self._head)
        if remaining_head > 0:
            self._head += text[:remaining_head]
            text = text[remaining_head:]

        tail_chars = self._limit.max_chars - self._limit.head_chars
        if tail_chars > 0 and text:
            self._tail = (self._tail + text)[-tail_chars:]

    def finish(self) -> CapturedOutput:
        truncated = self._total_chars > self._limit.max_chars
        omitted_chars = max(0, self._total_chars - self._limit.max_chars)
        if truncated:
            marker = f"\n... [截断 {omitted_chars} 字符] ...\n"
            content = self._head + marker + self._tail
        else:
            content = self._head + self._tail
        return CapturedOutput(
            content=content,
            total_chars=self._total_chars,
            truncated=truncated,
            omitted_chars=omitted_chars,
        )


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
