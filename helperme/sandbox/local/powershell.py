from __future__ import annotations

import codecs
import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

from helperme.sandbox.command import (
    CaptureLimit,
    CapturedOutput,
    CommandResult,
    CommandStartError,
    ShellNotFoundError,
)


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
            **dict({} if fixed_values is None else fixed_values),
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


class PowerShellCommandRunner:
    """在固定 PowerShell 中执行单次前台、非交互命令。"""

    def __init__(
        self,
        executable: str | None = None,
        environment_policy: CommandEnvironmentPolicy | None = None,
        capture_limit: CaptureLimit | None = None,
    ) -> None:
        if executable is not None and not executable.strip():
            raise ValueError("PowerShell executable 不能为空")
        if executable is None:
            for candidate in ("pwsh.exe", "powershell.exe"):
                resolved = shutil.which(candidate)
                if resolved is not None:
                    executable = resolved
                    break
            else:
                raise ShellNotFoundError(
                    "powershell",
                    "pwsh.exe / powershell.exe",
                )
        self.executable = executable
        self.environment_policy = (
            CommandEnvironmentPolicy()
            if environment_policy is None
            else environment_policy
        )
        self.capture_limit = (
            CaptureLimit() if capture_limit is None else capture_limit
        )

    async def run(
        self,
        command: str,
        cwd: Path,
        timeout_seconds: int,
    ) -> CommandResult:
        executable = shutil.which(self.executable)
        if executable is None:
            raise ShellNotFoundError("powershell", self.executable)

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
            proc = await asyncio.create_subprocess_exec(
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                utf8_command,
                cwd=cwd,
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise CommandStartError(str(exc)) from exc
        assert proc.stdout is not None
        assert proc.stderr is not None

        stdout_capture = _BoundedTextCapture(self.capture_limit)
        stderr_capture = _BoundedTextCapture(self.capture_limit)
        async def drain(stream, capture: _BoundedTextCapture) -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while chunk := await stream.read(4_096):
                capture.feed(decoder.decode(chunk))
            capture.feed(decoder.decode(b"", final=True))

        readers = (
            asyncio.create_task(drain(proc.stdout, stdout_capture)),
            asyncio.create_task(drain(proc.stderr, stderr_capture)),
        )

        async def terminate_and_drain() -> None:
            if proc.returncode is None:
                if os.name == "nt":
                    try:
                        killer = await asyncio.create_subprocess_exec(
                            "taskkill",
                            "/PID",
                            str(proc.pid),
                            "/T",
                            "/F",
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await killer.wait()
                    except OSError:
                        pass
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await proc.wait()
            await asyncio.gather(*readers)

        timed_out = False
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout_seconds)
            except TimeoutError:
                timed_out = True
                await terminate_and_drain()
            else:
                await asyncio.gather(*readers)
        except BaseException:
            cleanup = asyncio.create_task(terminate_and_drain())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise

        return CommandResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=stdout_capture.finish(),
            stderr=stderr_capture.finish(),
            duration_ms=round((time.perf_counter() - started) * 1_000),
            timed_out=timed_out,
        )
