from pydantic import BaseModel, Field
from core.tool_registry import register_tool, EmptyInput
from typing import Any, Literal
from pathlib import Path
import sys
from python_ripgrep import search
import subprocess
import shutil

# 简单的worksplace
WORKSPACE = Path(__file__).resolve().parent.parent
# tools/file_search.py → tools/ → helperMe/

class GlobInput(BaseModel):
    pattern: str = Field(description="作用模式，glob,对应 -g 参数")
    path: str = Field(default=".", description="搜索目录路径，相对worksplace的路径")
    kind: Literal["file", "directory", "any"] = Field(default="any", description="搜索类型，文件或目录或都要")
    max_depth: int | None = Field(default=None, description="最大搜索深度，默认null，递归查找")
    max_results: int = Field(default=10, description="最多返回结果数")

class ReadFileInput(BaseModel):
    path: str = Field(description="文件路径，相对路径或绝对路径")
    offset: int = Field(default=1, description="读取起始行号，从 1 开始")
    limit: int = Field(default=200, description="最多读取行数")

class SearchTextsInput(BaseModel):
    query: str = Field(description="搜索关键词或正则表达式")
    path: str = Field(description="搜索目录路径，相对路径或绝对路径")
    max_results: int = Field(default=10, description="最多返回结果数")
    context_lines: int = Field(default=2, description="每个匹配前后各返回多少行上下文")

    
def _resolve_in_workspace(path: str) -> tuple[Path | None, dict[str, Any] | None]:
    """
    返回 (resolved_path, None) 或 (None, error_dict)
    """
    p = Path(path)
    if not p.is_absolute():
        p = WORKSPACE / p
    try:
        p = p.resolve()
    except OSError as e:
        return None, {"error": f"路径无法解析: {path}, {e}"}

    ws = WORKSPACE.resolve()
    if p != ws and ws not in p.parents and p not in ws.parents:
        # 更稳的写法：try p.relative_to(ws)
        try:
            p.relative_to(ws)
        except ValueError:
            return None, {"error": f"路径越界 workspace: {p.as_posix()}"}

    if not p.exists():
        return None, {"error": f"路径不存在: {p.as_posix()}"}
    if not p.is_dir():
        return None, {"error": f"不是目录: {p.as_posix()}"}

    return p, None

@register_tool("""
按文件名/路径 glob 模式在 workspace 内查找文件或目录。底层使用 fd。
不要用本工具搜索文件内容；搜内容请用 grep/search_texts。

适用场景：
- 浏览某层目录 → pattern="*", path=".", max_depth=1
- 只看子目录 → pattern="*", kind="directory", max_depth=1
- 找所有 Python 文件 → pattern="*.py", path="."
- 找指定文件名 → pattern="tool_registry.py", path="."

输入：
  pattern  glob 模式（必填），如 *、*.py、**/main.py
  path     搜索起始目录，相对 workspace，默认 "."
  kind     file | directory | any，默认 any
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

    root, err = _resolve_in_workspace(raw.path)
    if err:
        return err

    cmd = ["fd", "-g", raw.pattern, "-a", str(root)]
    if raw.max_depth is not None:
        cmd.extend(["--max-depth", str(raw.max_depth)])
    if raw.kind == "directory":
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
        entry_kind = "directory" if abs_path.is_dir() else "file"
        matches.append({"path": rel, "kind": entry_kind})
    return {
        "pattern": raw.pattern,
        "path": raw.path,
        "matches": matches,
        "total": total,
        "truncated": truncated, 
    }


@register_tool("""
获取当前agent所在的工作区的绝对路径和系统平台。

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


def _parse_rg_line(line: str) -> dict | None:
    line = line.rstrip("\r\n")
    for sep, kind in ((":", "match"), ("-", "context")):
        parts = line.rsplit(sep, 2)
        if len(parts) == 3 and parts[1].isdigit():
            return {
                "file": parts[0],
                "line": int(parts[1]),
                "content": parts[2],
                "kind": kind,
            }
    return None

@register_tool("""
根据关键词或正则表达式搜索文件内容。
适用场景：用户或agent需要根据关键词或正则表达式搜索文件内容。
输入：
    query，搜索关键词或正则表达式。
    path，搜索目录路径，相对路径或绝对路径，默认 "."。
    max_results，最多返回结果数，默认 10。
    context_lines，每个匹配前后各返回多少行上下文，默认 2；需要看函数定义/类结构时建议 3~5。

输出 hits 中每项：
    file — 文件路径
    line — 匹配行号
    snippet — 按行序排列的片段，kind 为 "match" 或 "context"    
""", input_model=SearchTextsInput)
def search_texts(raw: SearchTextsInput) -> dict[str, Any]:
    p = Path(raw.path)
    if not p.is_absolute():
        p = WORKSPACE / p

    raw_results = search(
        patterns=[raw.query],
        paths=[str(p)],
        line_number=True,
        max_count=raw.max_results,
        before_context=raw.context_lines,
        after_context=raw.context_lines,
    )

    hits = []
    for block in raw_results:
        lines = []
        match_line = None
        for line in block.splitlines():
            parsed = _parse_rg_line(line)
            if not parsed:
                continue
            lines.append(parsed)
            if parsed["kind"] == "match":
                match_line = parsed["line"]

        if lines:
            hits.append({
                "file": lines[0]["file"],
                "line": match_line,
                "snippet": lines,  # 含 match + context，按行序排列
            })

    return {
        "path": p.as_posix(),
        "query": raw.query,
        "context_lines": raw.context_lines,
        "hits": hits,
        "total_hits": len(hits),
        "truncated": len(hits) >= raw.max_results,
    }