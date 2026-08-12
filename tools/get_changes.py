from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.tool_registry import ToolSpec, PydanticParameters
from tools.workspace import (
    WorkspaceInputError,
    WorkspaceSandboxes,
    workspace_error,
)


GET_CHANGES_DESCRIPTION = """
用途：读取指定 workspace root 的 Git 状态与未暂存差异，用于验证文件修改和最终总结。
何时使用：修改后核对磁盘实际变化、最终回答前确认声明与 diff 一致时使用；它只验证，不代替 apply_patch/replace_all/write_file，也不用于查找文件或内容。
关键限制：root 必须是已配置名称，path 必须相对 root；只支持 Git 工作区；status 可显示 staged、unstaged 和 untracked 路径，但 diff 只包含 tracked 文件的 unstaged 正文差异，不能据此声称已核对 staged 或 untracked 文件的具体内容。
失败/截断后：当前结果不截断；VERIFICATION_BACKEND_UNAVAILABLE 时应明确无法用 Git 验证，不能声称无改动；GIT_CHANGES_FAILED 时保留失败事实并修复后端后再核对。
""".strip()


class GetChangesInput(BaseModel):
    root: str = Field(description="workspace root 的逻辑名称；只能使用 get_workspace_info 返回的 name")
    path: str | None = Field(default=None, description="root 内可选的相对文件或目录路径；不传则检查整个 root")


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


def create_get_changes_specs(workspaces: WorkspaceSandboxes) -> list[ToolSpec]:
    async def get_changes(raw: GetChangesInput) -> dict[str, Any]:
        try:
            sandbox = workspaces.get(raw.root)
            target = sandbox.resolve(raw.path) if raw.path is not None else None
        except WorkspaceInputError as exc:
            return workspace_error(exc)

        repo_returncode, _, _ = await _run_git(
            ["rev-parse", "--is-inside-work-tree"],
            sandbox.root,
        )
        if repo_returncode != 0:
            return {
                "ok": False,
                "code": "VERIFICATION_BACKEND_UNAVAILABLE",
                "source": "no_git_backend",
                "changed": None,
                "status": "",
                "diff": "",
                "truncated": False,
                "message": "当前 workspace root 不是 git 仓库。",
            }

        path_args = ["--", sandbox.relative(target)] if target is not None else []
        git_results = await asyncio.gather(
            _run_git(
                ["status", "--short", "--untracked-files=all", *path_args],
                sandbox.root,
            ),
            _run_git(["diff", *path_args], sandbox.root),
            return_exceptions=True,
        )
        for result in git_results:
            if isinstance(result, BaseException):
                raise result
        status_result, diff_result = git_results
        status_returncode, status, _ = status_result
        diff_returncode, diff, _ = diff_result
        ok = status_returncode == 0 and diff_returncode == 0
        return {
            "ok": ok,
            "code": "CHANGES_READ" if ok else "GIT_CHANGES_FAILED",
            "root": raw.root,
            "source": "git",
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
