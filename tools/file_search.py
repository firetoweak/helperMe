from pydantic import BaseModel, Field
from core.tool_registry import register_tool, EmptyInput
from typing import Any
from pathlib import Path
import sys

# 简单的worksplace
WORKSPACE = Path(__file__).resolve().parent.parent
# tools/file_search.py → tools/ → helperMe/

class ListFilesInput(BaseModel):
    path: str = Field(description="目录路径，相对路径或绝对路径")


@register_tool("""
获取当前工作区的绝对路径和系统平台。

适用场景：用户询问当前的目录或者你需要了解当前的工作环境。
输入：无参数，传 {} 即可。
输出：JSON 字符串，字段含义如下：
      - workspace: 工作区的绝对路径
      - platform: 系统平台
""")
def get_workspace_info(_: EmptyInput) -> dict[str, Any]:
    return {
        "workspace_root": WORKSPACE.resolve().as_posix(),
        "platform": sys.platform,
    }


@register_tool("""
列出指定目录下的所有文件。

适用场景：用户或agent需要了解当前目录下的文件列表。
输入：path，目录路径。
输出：JSON 字符串，字段含义如下：
      - path: 目录路径
      - entries: 目录下的所有文件和目录，以相对路径的形式列出
""", input_model=ListFilesInput)
def list_files(raw: ListFilesInput) -> dict[str, Any]:
    p = Path(raw.path)
    if not p.is_absolute():
        p = WORKSPACE / p

    return {
        "path": p.resolve().as_posix(),
        "entries": [
            {
                "name": entry.name,
                "type": "file" if entry.is_file() else "directory",
                "path": entry.relative_to(p).as_posix(),
            }
            for entry in p.iterdir()
        ],
    }