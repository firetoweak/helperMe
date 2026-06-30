from tools.workspace import _resolve_in_workspace, _to_workspace_relative
from pydantic import BaseModel, Field
from core.tool_registry import register_tool
from typing import Any

class WriteFileInput(BaseModel):
    path: str = Field(description="要写入的文件路径，相对workspace的路径，必须包含文件名，例如 notes/todo.txt")
    content: str = Field(default="", description="要写入的完整文件内容；为空字符串表示创建空文件")
    overwrite: bool = Field(default=False, description="文件已存在时是否覆盖。false=拒绝覆盖，true=整体覆盖")

@register_tool("""
在 workspace 内创建文本文件，或在明确允许时整体覆盖已有文件。

适用场景：
- 新建文章、计划、草稿、报告、笔记等文本文件
- 用户明确要求“重写/覆盖整个文件”

不适用场景：
- 只改文件局部内容；用 apply_patch
- 查找/读取文件；用 glob/read_file/grep

行为：
- path 是相对 workspace 的文件路径，必须包含文件名，例如 docs/plan.md
- 文件不存在：创建文件
- 文件已存在且 overwrite=false：拒绝覆盖，返回错误
- 文件已存在且 overwrite=true：用 content 整体覆盖
- 父目录不存在时可以自动创建
- 只能写入 workspace 内路径，禁止写到 workspace 外

输入：
- path: workspace 相对路径，必须包含文件名，如 docs/plan.md
- content: 要写入的完整文本，默认空字符串
- overwrite: 是否允许覆盖已有文件，默认 false

失败处理：
- FILE_EXISTS: 文件已存在。除非用户明确要求覆盖，否则不要重试 overwrite=true
- IS_A_DIR: path 指向目录，应补充文件名
- PATH_OUTSIDE_WORKSPACE: 路径越界，改用 workspace 相对路径
""", input_model=WriteFileInput)
def write_file(raw: WriteFileInput) -> dict[str, Any]:
    p, err = _resolve_in_workspace(path=raw.path, must_exist=False, create_parents=True)
    if err:
        return {"ok":False, **err}
    rel = _to_workspace_relative(p)
    if p.exists() and p.is_dir():
        return {"ok": False, "code": "IS_A_DIR", "path": rel}
    
    existed = p.exists() 
    if existed and not raw.overwrite:
        return {"ok": False, "code": "FILE_EXISTS", "path": rel}
    
    try:
        p.write_text(raw.content, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "code": "WRITE_FAILED", "error": str(e), "path": rel}
    
    return {
    "ok": True,
    "code": "FILE_OVERWRITTEN" if existed else "FILE_CREATED",
    "path": rel,
    }