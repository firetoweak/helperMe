from pydantic import BaseModel, Field
from core.tool_registry import register_tool, EmptyInput
from typing import Any, Literal
from tools.workspace import _resolve_in_workspace, _to_workspace_relative, WORKSPACE
import subprocess


class GetChangesInput(BaseModel):
    path: str | None = Field(default=None, description="可选，要查看改动的文件或目录路径；不传则查看整个 workspace")


@register_tool("""
查看当前 workspace 的实际文件改动，用于修改后的验证和最终总结前核对。
适用场景：
- 修改文件后，确认磁盘上真实发生了哪些变化
- 最终回复用户前，核对总结是否与实际改动一致
- 发现计划修改和实际 diff 不一致时，诚实说明未完成部分
不适用场景：
- 修改文件；用 apply_patch、replace_all 或 write_file
- 查找文件或内容；用 glob、grep 或 read_file
使用提示：
- 不传 path 时查看整个 workspace 的改动
- 传 path 时只查看某个文件或目录的改动
- 总结修改内容时，只能基于 status 和 diff 中真实出现的变化
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