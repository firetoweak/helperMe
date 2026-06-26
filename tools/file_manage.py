from tools.workspace import _resolve_in_workspace, _to_workspace_relative
from pydantic import BaseModel, Field
from core.tool_registry import register_tool
from typing import Any

class WriteFileInput(BaseModel):
    path: str = Field(description="要写入的文件路径，相对worksplace的路径，例如 notes/todo.txt")
    content: str = Field(default="", description="要写入的完整文件内容；为空字符串表示创建空文件")
    overwrite: bool = Field(default=False, description="文件已存在时是否覆盖。false=拒绝覆盖，true=整体覆盖")

@register_tool("""
在 workspace 内创建或整体写入文本文件。

适用场景：
- 撰写文章、计划、草稿或报告
- 保存搜索到的资料或记录笔记
- 将完整内容写入一个新文件
- 明确需要整体覆盖已有文件

不适用场景：
- 只修改已有文件的一小段内容；这种情况请用 apply_patch
- 搜索文件名；请用 glob
- 搜索文件内容；请用 grep
- 读取文件内容；请用 read_file

行为：
- path 是相对 workspace 的文件路径，必须包含文件名，例如 docs/plan.md
- 文件不存在：创建文件
- 文件已存在且 overwrite=false：拒绝覆盖，返回错误
- 文件已存在且 overwrite=true：用 content 整体覆盖
- 父目录不存在时可以自动创建
- 只能写入 workspace 内路径，禁止写到 workspace 外

输入：
  path       相对 workspace 的文件路径，包含文件名
  content    要写入的完整文本内容，默认空字符串
  overwrite  是否允许覆盖已有文件，默认 false

输出：
  ok         是否修改成功
  code       结果码
  path       修改文件路径

结果码：
FILE_CREATED        新建成功
FILE_OVERWRITTEN    覆盖成功
FILE_EXISTS         已存在且 overwrite=false
IS_A_DIR            路径是目录
WRITE_FAILED        写入失败
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