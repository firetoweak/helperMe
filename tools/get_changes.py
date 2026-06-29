from pydantic import BaseModel, Field
from core.tool_registry import register_tool, EmptyInput
from typing import Any, Literal
from tools.workspace import _resolve_in_workspace, _to_workspace_relative, WORKSPACE
import subprocess


class GetChangesInput(BaseModel):
    path: str | None = Field(default=None, description="可选，只查看某个文件或目录的改动")


@register_tool("""
查看当前工作区的实际文件改动。
适用场景：
- 修改文件后，确认磁盘上到底发生了哪些变化
- 最终回复用户前，核对总结是否与真实改动一致
- 避免把计划中的修改误说成已经完成的修改
输入：
- path：可选，只查看某个文件或目录的改动；不传则查看整个工作区
输出：
- ok：工具是否正常执行
- source：变更来源，第一版仅支持使用 git
- changed：是否检测到改动
- status：文件变更列表
- diff：具体文本差异
""", input_model=GetChangesInput)
def get_changes(raw: GetChangesInput) -> dict[str, Any]:
    target = None
    if raw.path:
        target, err = _resolve_in_workspace(raw.path, must_exist=False)
        if err:
            return {"ok": False, **err}
    
    repo_check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if repo_check.returncode != 0:
        return {
            "ok": True,
            "source": "no_git_backend",
            "changed": None,
            "status": "",
            "diff": "",
            "truncated": False,
            "message": "当前 workspace 不是 git 仓库，第一版 get_changes 暂时无法生成变更对比。后续需要 snapshot backend。",
        }

    path_args = []
    if target is not None:
        path_args = ["--", _to_workspace_relative(target)]

    status_proc = subprocess.run(
        ["git", "status", "--short", *path_args],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    diff_proc = subprocess.run(
        ["git", "diff", *path_args],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    status = status_proc.stdout
    diff = diff_proc.stdout
    return {
        "ok": status_proc.returncode == 0 and diff_proc.returncode == 0,
        "source": "git",
        "changed": bool(status.strip() or diff.strip()),
        "status": status,
        "diff": diff,
        "truncated": False,
    }