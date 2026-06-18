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

class ReadFileInput(BaseModel):
    path: str = Field(description="文件路径，相对路径或绝对路径")
    offset: int = Field(default=1, description="读取起始行号，从 1 开始")
    limit: int = Field(default=200, description="最多读取行数")

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
    res = {"path": p.as_posix(), "entries": []}
    for entry in p.iterdir():
        res["entries"].append({
            "name": entry.name,
            "type": "file" if entry.is_file() else "directory",
            "path": entry.relative_to(p).as_posix(),
        })
    return res

# 读文件的工具的话，需要限制读的长度，不能读太长
@register_tool("""
读文件内容，
适用场景：用户或agent需要读取文件内容。
输入：
    path，文件路径。
    offset，读取起始行号，从 1 开始。
    limit，最多读取行数。
输出：JSON 字符串，字段含义如下：
      - path: 文件路径
      - content: 文件内容
      - start_line: 读取起始行号
      - end_line: 读取结束行号
      - total_lines: 文件总行数
      - next_offset: 下一页的读取起始行号
      - truncated: 是否截断
限制：
    最多读取 3000 字符，超出会截断
""", input_model=ReadFileInput)
def read_file(raw: ReadFileInput) -> dict[str, Any]:
    max_length= 3000 # 默认最大读取字符数，超出会截断
    truncated = False
    p = Path(raw.path)
    if not p.is_absolute():
        p = WORKSPACE / p
    if not p.exists():
        return {"error": f"路径不存在: {p.as_posix()}"}
    if not p.is_file():
        return {"error": f"不是文件: {p.as_posix()}"}
    try:    
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
            total_lines = len(lines)
            if raw.offset > total_lines:
                return {
                    "error": f"起始行号超出文件总行数: {p.as_posix()}",
                    "total_lines": total_lines,
                    "path":p.as_posix(),
                }
            start_line = raw.offset - 1
            end_line = min(start_line + raw.limit, total_lines)
            selected_lines = lines[start_line:end_line]
            end_line = start_line + len(selected_lines)

            content = "".join(selected_lines)

            if len(content) > max_length:
                content = content[:max_length]
                truncated = True

            if end_line < total_lines:
                next_offset = end_line + 1
            else:
                next_offset = None
            return {
                "path": p.as_posix(),
                "content": content,
                "start_line": start_line + 1,
                "end_line": end_line,
                "total_lines": total_lines,
                "next_offset": next_offset,
                "truncated": truncated,
            }
    except UnicodeDecodeError:
        return {"error": f"无法以文本读取（可能是二进制文件）: {p.as_posix()}"}
    except OSError as e:
        return {"error": f"文件系统错误: {e}"}