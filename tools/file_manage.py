from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.environment import (
    EnvironmentBinding,
    EnvironmentInputError,
    environment_error,
)
from core.tool_registry import ToolSpec, PydanticParameters


WRITE_FILE_DESCRIPTION = """
用途：在当前 Environment Workspace View 内创建文本文件，或在明确允许时整体覆盖已有文件；父目录不存在时自动创建。
何时使用：新建文章、代码、计划等完整文件，或用户明确要求重写整个文件时使用；局部修改用 apply_patch，批量替换用 replace_all，查找和读取用 glob/grep/read_file。
关键限制：相对 path 基于当前 Turn cwd，绝对 path 使用 Environment 原生语义；overwrite=false 时绝不覆盖已有文件。
失败/截断后：结果不截断；FILE_EXISTS 后不要擅自改为 overwrite=true，应改用局部编辑或取得覆盖授权；IS_A_DIR 时补充文件名；路径错误时修正 path 后重试。
""".strip()


class WriteFileInput(BaseModel):
    path: str = Field(description="要写入的文件路径；相对路径基于当前 Turn cwd")
    content: str = Field(description="要写入的完整文本内容；显式传入空字符串表示创建空文件")
    overwrite: bool = Field(default=False, description="文件已存在时是否整体覆盖；false 拒绝覆盖，true 完整覆盖")


def create_file_manage_specs(binding: EnvironmentBinding) -> list[ToolSpec]:
    resolver = binding.resolver

    async def write_file(raw: WriteFileInput) -> dict[str, Any]:
        try:
            resolved_path = resolver.resolve(raw.path, access="write")
        except EnvironmentInputError as exc:
            return environment_error(exc)

        path = resolved_path.native_path
        relative_path = resolved_path.workspace_membership.display_path
        if path.exists() and path.is_dir():
            return {
                "ok": False,
                "code": "IS_A_DIR",
                "path": relative_path,
                **resolved_path.result_fields(),
            }
        existed = path.exists()
        if existed and not raw.overwrite:
            return {
                "ok": False,
                "code": "FILE_EXISTS",
                "path": relative_path,
                **resolved_path.result_fields(),
            }

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw.content, encoding="utf-8")
        return {
            "ok": True,
            "code": "FILE_OVERWRITTEN" if existed else "FILE_CREATED",
            "path": relative_path,
            **resolved_path.result_fields(),
        }

    return [
        ToolSpec(
            name="write_file",
            description=WRITE_FILE_DESCRIPTION,
            parameters=PydanticParameters(WriteFileInput),
            handler=write_file,
        )
    ]
