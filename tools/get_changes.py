from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel, Field

from core.tool_registry import ToolSpec
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


def create_get_changes_specs(workspaces: WorkspaceSandboxes) -> list[ToolSpec]:
    def get_changes(raw: GetChangesInput) -> dict[str, Any]:
        try:
            sandbox = workspaces.get(raw.root)
            target = sandbox.resolve(raw.path) if raw.path is not None else None
        except WorkspaceInputError as exc:
            return workspace_error(exc)

        repo_check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=sandbox.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if repo_check.returncode != 0:
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
        status_proc = subprocess.run(
            ["git", "status", "--short", *path_args],
            cwd=sandbox.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        diff_proc = subprocess.run(
            ["git", "diff", *path_args],
            cwd=sandbox.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        ok = status_proc.returncode == 0 and diff_proc.returncode == 0
        status = status_proc.stdout
        diff = diff_proc.stdout
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
            input_model=GetChangesInput,
            handler=get_changes,
        )
    ]
