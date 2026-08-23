from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from helperme.sandbox.api import (
    EnvironmentBinding,
    environment_error,
)
from helperme.sandbox.workspace import EnvironmentInputError
from helperme.tools.spec import PydanticParameters, ToolSpec


GET_CHANGES_DESCRIPTION = """
用途：读取当前 Environment 中指定路径所属 Git 工作区的状态与未暂存差异，用于验证文件修改和最终总结。
何时使用：修改后核对磁盘实际变化、最终回答前确认声明与 diff 一致时使用；它只验证，不代替 apply_patch/replace_all/write_file，也不用于查找文件或内容。
关键限制：相对 path 基于当前 Environment cwd，绝对 path 使用 Environment 原生语义；只支持 Git 工作区；status 可显示 staged、unstaged 和 untracked 路径，但 diff 只包含 tracked 文件的 unstaged正文差异。
失败/截断后：当前结果不截断；VERIFICATION_BACKEND_UNAVAILABLE 时应明确无法用 Git 验证，不能声称无改动；GIT_CHANGES_FAILED 时保留失败事实并修复后端后再核对。
""".strip()


class GetChangesInput(BaseModel):
    path: str = Field(default=".", description="要检查的文件或目录；相对路径基于当前 Environment cwd")


async def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        raise
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def create_get_changes_specs(binding: EnvironmentBinding) -> list[ToolSpec]:
    resolver = binding.resolver

    async def get_changes(raw: GetChangesInput) -> dict[str, Any]:
        try:
            resolved_target = resolver.resolve(raw.path)
        except EnvironmentInputError as exc:
            return environment_error(exc)
        target = resolved_target.native_path
        try:
            discovery_cwd = target if target.is_dir() else target.parent
        except OSError as exc:
            return _git_failure(resolved_target, exc)

        try:
            repo_returncode, repo_stdout, _ = await _run_git(
                ["rev-parse", "--show-toplevel"],
                discovery_cwd,
            )
        except OSError as exc:
            return _git_failure(resolved_target, exc)
        if repo_returncode != 0:
            return {
                "ok": False,
                "code": "VERIFICATION_BACKEND_UNAVAILABLE",
                "source": "no_git_backend",
                "changed": None,
                "status": "",
                "diff": "",
                "truncated": False,
                **resolved_target.result_fields(),
                "message": "当前路径不属于 Git 工作区。",
            }

        try:
            repo_path = repo_stdout.strip()
            if not repo_path:
                raise ValueError("git rev-parse 返回空路径")
            resolved_repo = resolver.resolve(repo_path)
        except EnvironmentInputError as exc:
            return environment_error(exc)
        repo_root = resolved_repo.native_path
        try:
            path_args = ["--", target.relative_to(repo_root).as_posix()]
        except ValueError as exc:
            return _git_failure(resolved_target, exc)
        git_results = await asyncio.gather(
            _run_git(
                ["status", "--short", "--untracked-files=all", *path_args],
                repo_root,
            ),
            _run_git(["diff", *path_args], repo_root),
            return_exceptions=True,
        )
        errors = [
            result
            for result in git_results
            if isinstance(result, BaseException)
        ]
        unexpected = [error for error in errors if not isinstance(error, OSError)]
        if len(unexpected) == 1:
            raise unexpected[0]
        if unexpected:
            raise BaseExceptionGroup("多个 Git 验证命令异常", unexpected)
        if errors:
            return _git_failure(resolved_target, errors[0])
        status_result, diff_result = git_results
        status_returncode, status, _ = status_result
        diff_returncode, diff, _ = diff_result
        ok = status_returncode == 0 and diff_returncode == 0
        return {
            "ok": ok,
            "code": "CHANGES_READ" if ok else "GIT_CHANGES_FAILED",
            "source": "git",
            **resolved_target.result_fields(),
            "repository_location": resolved_repo.location.to_dict(),
            "changed": bool(status.strip() or diff.strip()),
            "status": status,
            "diff": diff,
            "truncated": False,
        }

    return [
        ToolSpec(
            name="get_changes",
            description=GET_CHANGES_DESCRIPTION,
            parameters=PydanticParameters(GetChangesInput),
            handler=get_changes,
        )
    ]


def _git_failure(resolved_target, exc: BaseException) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "GIT_CHANGES_FAILED",
        "source": "git",
        "changed": None,
        "status": "",
        "diff": "",
        "truncated": False,
        **resolved_target.result_fields(),
        "message": f"Git 验证失败: {type(exc).__name__}: {exc}",
    }
