from __future__ import annotations

import codecs
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_ENV_NAMES = (
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PSMODULEPATH",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)


@dataclass(frozen=True)
class CaptureLimit:
    max_chars: int = 6_000
    head_chars: int = 2_400

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("max_chars 必须大于 0")
        if not 0 <= self.head_chars <= self.max_chars:
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


class PowerShellNotFoundError(FileNotFoundError):
    def __init__(self, executable: str) -> None:
        self.executable = executable
        super().__init__(f"未找到 PowerShell 可执行程序: {executable}")


class CommandStartError(OSError):
    pass


class CommandEnvironmentPolicy:
    """从宿主环境中选择明确允许传给命令子进程的变量。"""

    def __init__(
        self,
        forward_names: Sequence[str] = (),
        fixed_values: Mapping[str, str] | None = None,
    ) -> None:
        names = (*DEFAULT_ENV_NAMES, *forward_names)
        if any(not name or not name.strip() for name in names):
            raise ValueError("环境变量名称不能为空")
        self._forward_names = tuple(dict.fromkeys(name.casefold() for name in names))
        self._fixed_values = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            **dict(fixed_values or {}),
        }

    def build(self, host_env: Mapping[str, str]) -> dict[str, str]:
        indexed = {name.casefold(): (name, value) for name, value in host_env.items()}
        child_env = {
            indexed[name][0]: indexed[name][1]
            for name in self._forward_names
            if name in indexed
        }
        child_env.update(self._fixed_values)
        return child_env


class _BoundedTextCapture:
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


def _drain_stream(
    stream,
    capture: _BoundedTextCapture,
    errors: list[BaseException],
) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while chunk := stream.read(4_096):
            capture.feed(decoder.decode(chunk))
        capture.feed(decoder.decode(b"", final=True))
    except BaseException as exc:
        errors.append(exc)
    finally:
        stream.close()


def _terminate_process_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    if proc.poll() is None:
        proc.kill()
    proc.wait()


class PowerShellCommandRunner:
    """在固定 PowerShell 中执行单次前台、非交互命令。"""

    def __init__(
        self,
        executable: str = "powershell.exe",
        environment_policy: CommandEnvironmentPolicy | None = None,
        capture_limit: CaptureLimit | None = None,
    ) -> None:
        if not executable or not executable.strip():
            raise ValueError("PowerShell executable 不能为空")
        self.executable = executable
        self.environment_policy = environment_policy or CommandEnvironmentPolicy()
        self.capture_limit = capture_limit or CaptureLimit()

    def run(self, command: str, cwd: Path, timeout_seconds: int) -> CommandResult:
        executable = shutil.which(self.executable)
        if executable is None:
            raise PowerShellNotFoundError(self.executable)

        child_env = self.environment_policy.build(os.environ)
        utf8_command = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
            "$OutputEncoding=[Console]::OutputEncoding;"
            + command
        )
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        started = time.perf_counter()
        try:
            proc = subprocess.Popen(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    utf8_command,
                ],
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise CommandStartError(str(exc)) from exc
        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_capture = _BoundedTextCapture(self.capture_limit)
        stderr_capture = _BoundedTextCapture(self.capture_limit)
        reader_errors: list[BaseException] = []
        readers = (
            threading.Thread(
                target=_drain_stream,
                args=(proc.stdout, stdout_capture, reader_errors),
                name="command-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=_drain_stream,
                args=(proc.stderr, stderr_capture, reader_errors),
                name="command-stderr",
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(proc)

        for reader in readers:
            reader.join()
        if reader_errors:
            raise reader_errors[0]

        return CommandResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout_capture.finish(),
            stderr=stderr_capture.finish(),
            duration_ms=round((time.perf_counter() - started) * 1_000),
            timed_out=timed_out,
        )
