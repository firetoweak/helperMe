from pydantic import BaseModel, Field
from core.tool_registry import register_tool, EmptyInput
from typing import Any, Literal
from tools.workspace import _resolve_in_workspace, _to_workspace_relative, WORKSPACE
import sys
import subprocess
import shutil
import json
from pathlib import Path

class GlobInput(BaseModel):
    pattern: str = Field(description="作用模式，glob,对应 -g 参数")
    path: str = Field(default=".", description="搜索目录路径，相对worksplace的路径")
    kind: Literal["file", "dir", "any"] = Field(default="any", description="搜索类型，文件或目录或都要")
    max_depth: int | None = Field(default=None, description="最大搜索深度，默认null，递归查找")
    max_results: int = Field(default=10, description="最多返回结果数")

class ReadFileInput(BaseModel):
    path: str = Field(description="文件路径，相对worksplace的路径")
    offset: int = Field(default=1, description="读取起始行号，从 1 开始")
    limit: int = Field(default=200, description="最多读取行数")

class GrepInput(BaseModel):
    query: str = Field(description="搜索关键词或正则表达式")
    path: str = Field(default=".", description="搜索目录路径(必须是目录)，相对worksplace的路径")
    context_lines: int = Field(default=2, description="每个匹配前后各返回多少行上下文")
    max_results: int = Field(default=10, description="最多返回 match 条数")

class ApplyPatchInput(BaseModel):
    path: str = Field(description="文件路径，相对worksplace的路径")
    old_string: str = Field(description="要被修改替换的部分")
    new_string: str = Field(description="替换后的部分")
    


@register_tool("""
获取当前agent所在的workspace的绝对路径和系统平台。

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
按文件名/路径 glob 模式在 workspace 内查找文件或目录。底层使用 fd。
不要用本工具搜索文件内容；搜内容请用 grep。

适用场景：
- 浏览某层目录 → pattern="*", path=".", max_depth=1
- 只看子目录 → pattern="*", kind="dir", max_depth=1
- 找所有 Python 文件 → pattern="*.py", path="."
- 找指定文件名 → pattern="tool_registry.py", path="."

输入：
  pattern  glob 模式（必填），如 *、*.py、**/main.py
  path     搜索起始目录，相对 workspace，默认 "."
  kind     file | dir | any，默认 any
  max_depth  最大深度；null=递归；1=只看 path 下直接一层
  max_results  最多返回条数，默认 10

输出：
  pattern, path, matches[{path, kind}], total, truncated
  path 为相对 workspace 的路径，便于 read_file 直接使用
  error 字段表示路径不存在或越界 workspace
""", input_model=GlobInput)
def glob(raw: GlobInput) -> dict[str, Any]:
    if shutil.which("fd") is None:
        return {"error": "未找到 fd，请先安装: winget install sharkdp.fd"}

    root, err = _resolve_in_workspace(raw.path, expect="dir")
    if err:
        return err

    cmd = ["fd", "-g", raw.pattern, "-a", str(root)]
    if raw.max_depth is not None:
        cmd.extend(["--max-depth", str(raw.max_depth)])
    if raw.kind == "dir":
        cmd.extend(["-t", "d"])
    elif raw.kind == "file":
        cmd.extend(["-t", "f"])
    
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")

    if proc.returncode == 2:
        return {"error": proc.stderr.strip() or "fd 执行失败"}

    ws = WORKSPACE.resolve()
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    total = len(lines)
    truncated = total > raw.max_results
    lines = lines[:raw.max_results]
    matches = []
    for line in lines:
        abs_path = Path(line).resolve()
        rel = abs_path.relative_to(ws).as_posix()
        entry_kind = "dir" if abs_path.is_dir() else "file"
        matches.append({"path": rel, "kind": entry_kind})
    return {
        "pattern": raw.pattern,
        "path": raw.path,
        "matches": matches,
        "total": total,
        "truncated": truncated, 
    }


@register_tool("""
在 workspace 内按内容搜索文本（正则）。底层使用 rg --json。
不要用本工具找文件名；找文件请用 glob。

适用场景：
- 找类/函数定义 → query="class ToolSpec"
- 找符号引用 → query="register_tool"
- 需要看上下文 → context_lines=3~5

输入：query（必填）, path（默认 "."）, context_lines（默认 2）, max_results（默认 10）
输出：hits[{file, line, snippet}]，file 为相对 workspace 路径
无匹配时 hits=[]，不是 error
""", input_model=GrepInput)
def grep(raw: GrepInput) -> dict[str, Any]:
    if shutil.which("rg") is None:
        # 后续版本扩展成python 兜底方式
        return {"error": "未找到 rg，请先安装: winget install BurntSushi.ripgrep.MSVC"}

    root, err = _resolve_in_workspace(raw.path, expect="dir")

    if err:
        return err
    cmd = ["rg", "--json", "-C", str(raw.context_lines), raw.query, str(root)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")

    if proc.returncode == 2:
        return {"error": proc.stderr.strip() or "rg 执行失败"}
    
    hits = []
    current_file = None
    snippet = []
    match_line = None

    for raw_line in proc.stdout.splitlines():
        if not raw_line.strip():
            continue
        obj = json.loads(raw_line)

        event_type = obj.get("type")
        data = obj.get("data") or {}

        if event_type == "begin":
            current_file = data["path"]["text"]
            snippet = []
            match_line = None
        elif event_type in {"context", "match"}:
            line_number = data["line_number"]
            content = data["lines"]["text"].rstrip("\r\n")

            if event_type == "match" and match_line is None:
                match_line = line_number
            snippet.append({
                "line": line_number,
                "content": content,
                "kind": event_type,
            })
        elif event_type == "end":
            if snippet and match_line is not None:
                hits.append({
                    "file": _to_workspace_relative(current_file),
                    "line": match_line,
                    "snippet": snippet,
                })
                if len(hits) >= raw.max_results:
                    break

    return {
        "path": raw.path,
        "query": raw.query,
        "context_lines": raw.context_lines,
        "hits": hits[:raw.max_results],
        "total_hits": len(hits[:raw.max_results]),
        "truncated": len(hits) >= raw.max_results,
    }


# 读文件的工具的话，需要限制读的长度，不能读太长
@register_tool("""
读文件内容，
适用场景：用户或agent需要读取文件内容，适用于文本文件（.py .md .json .txt .yaml 等）。
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
    p, err = _resolve_in_workspace(raw.path, expect="file")
    if err:
        return err

    try:    
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
            total_lines = len(lines)
            if raw.offset > total_lines:
                return {
                    "error": f"起始行号超出文件总行数: {_to_workspace_relative(p)}",
                    "total_lines": total_lines,
                    "path":_to_workspace_relative(p),
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
                "path": _to_workspace_relative(p),
                "content": content,
                "start_line": start_line + 1,
                "end_line": end_line,
                "total_lines": total_lines,
                "next_offset": next_offset,
                "truncated": truncated,
            }
    except UnicodeDecodeError:
        return {"error": f"无法以文本读取（可能是二进制文件）: {_to_workspace_relative(p)}"}
    except OSError as e:
        return {"error": f"文件系统错误: {e}"}