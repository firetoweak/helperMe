from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.tool_registry import ToolSpec, PydanticParameters
from tools.workspace import (
    WorkspaceInputError,
    WorkspaceSandboxes,
    workspace_error,
)


WRITE_FILE_DESCRIPTION = """
用途：在指定 workspace root 内创建文本文件，或在明确允许时整体覆盖已有文件；父目录不存在时自动创建。
何时使用：新建文章、代码、计划等完整文件，或用户明确要求重写整个文件时使用；局部修改用 apply_patch，批量替换用 replace_all，查找和读取用 glob/grep/read_file。
关键限制：root 必须是已配置名称，path 必须是 root 内相对文件路径；overwrite=false 时绝不覆盖已有文件，只有明确允许整体覆盖时才能传 overwrite=true。
失败/截断后：结果不截断；FILE_EXISTS 后不要擅自改为 overwrite=true，应改用局部编辑或取得覆盖授权；IS_A_DIR 时补充文件名；路径错误时修正 root/path 后重试。
""".strip()


class WriteFileInput(BaseModel):
    root: str = Field(description="workspace root 的逻辑名称；只能使用 get_workspace_info 返回的 name")
    path: str = Field(description="root 内要写入的相对文件路径，必须包含文件名，例如 notes/todo.txt")
    content: str = Field(description="要写入的完整文本内容；显式传入空字符串表示创建空文件")
    overwrite: bool = Field(default=False, description="文件已存在时是否整体覆盖；false 拒绝覆盖，true 完整覆盖")


def create_file_manage_specs(workspaces: WorkspaceSandboxes) -> list[ToolSpec]:
    async def write_file(raw: WriteFileInput) -> dict[str, Any]:
        try:
            sandbox = workspaces.get(raw.root)
            path = sandbox.resolve(raw.path)
        except WorkspaceInputError as exc:
            return workspace_error(exc)

        relative_path = sandbox.relative(path)
        if path.exists() and path.is_dir():
            return {"ok": False, "code": "IS_A_DIR", "path": relative_path}
        existed = path.exists()
        if existed and not raw.overwrite:
            return {"ok": False, "code": "FILE_EXISTS", "path": relative_path}

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw.content, encoding="utf-8")
        return {
            "ok": True,
            "code": "FILE_OVERWRITTEN" if existed else "FILE_CREATED",
            "root": raw.root,
            "path": relative_path,
        }

    return [
        ToolSpec(
            name="write_file",
            description=WRITE_FILE_DESCRIPTION,
            parameters=PydanticParameters(WriteFileInput),
            handler=write_file,
        )
    ]
