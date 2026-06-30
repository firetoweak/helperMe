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
    pattern: str = Field(description="文件名匹配模式，例如 *.py、docs/*.md")
    path: str = Field(default=".", description="搜索起始目录，相对 workspace，默认.")
    kind: Literal["file", "dir", "any"] = Field(default="any", description="查找类型：file（仅文件）、dir（仅文件夹）、any（都要）")
    max_depth: int | None = Field(default=None, description="搜索深度限制；默认（递归查找）；设为 1 则只看当前目录")
    max_results: int = Field(default=10, description="最多返回结果数量")

class ReadFileInput(BaseModel):
    path: str = Field(description="要读取的文件路径，相对 workspace 的路径")
    offset: int = Field(default=1, description="读取起始行号，从 1 开始")
    limit: int = Field(default=200, description="最多读取行数")

class GrepInput(BaseModel):
    query: str = Field(description="搜索关键词或正则表达式")
    path: str = Field(default=".", description="搜索目录路径(必须是目录)，相对worksplace的路径")
    context_lines: int = Field(default=2, description="每个匹配前后各返回多少行上下文")
    max_results: int = Field(default=10, description="最多返回 match 条数")


@register_tool("""
获取当前工作区的绝对路径和系统平台信息。

适用场景：
- 用户询问文件存放在哪里，或需要了解当前工作目录的位置
- 整理、归档文件，或需要在特定目录下执行操作
- 识别当前操作系统平台（如 Windows、Linux、macOS），以便执行平台特定的操作
- 编写报告、整理资源时需要引用项目路径信息

输入：无参数，传 {} 即可。
输出：JSON 对象，字段含义如下：
      - workspace_root: 工作区的绝对路径
      - platform: 系统平台标识（如 'linux', 'win32' 等）
""")
def get_workspace_info(_: EmptyInput) -> dict[str, Any]:
    return {
        "workspace_root": WORKSPACE.resolve().as_posix(),
        "platform": sys.platform,
    }


@register_tool("""
在 workspace 中按文件名模式查找文件或目录，返回可继续传给 read_file/grep/write_file 的相对路径。
适用场景：
- 不确定目标文件在哪里
- 需要按扩展名、文件名片段或目录层级定位资源
不适用场景：
- 搜索文件内容；用 grep
- 读取文件内容；用 read_file
使用提示：
- pattern 是文件名 glob 模式，例如 *.py、README.md、docs/*.md
- 结果过多时，缩小 path、kind、max_depth 或 max_results
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
在 workspace 内按关键词或正则搜索文本内容，返回匹配位置及少量上下文。
不要用本工具找文件名；找文件请用 glob。
适用场景：
- 不确定关键词出现在哪个文件
- 定位文章、代码或报告中的特定段落
- 修改前先找到包含目标内容的位置
- 需要少量上下文判断是否值得继续 read_file
不适用场景：
- 按文件名、扩展名或目录查找路径；用 glob
- 阅读已知文件的大段内容；用 read_file
使用提示：
- query 可以是关键词或正则表达式
- context_lines 用于控制每个匹配前后的上下文行数
- grep 只能定位；需要完整理解或修改前，应再用 read_file 读取目标区域
""", input_model=GrepInput)
def grep(raw: GrepInput) -> dict[str, Any]:
    if shutil.which("rg") is None:
        # 后续版本扩展成python 兜底方式
        return {"error": "未找到 rg，请先安装: winget install BurntSushi.ripgrep.MSVC"}

    p, err = _resolve_in_workspace(raw.path, expect="any")

    if err:
        return err
    cmd = ["rg", "--json", "-C", str(raw.context_lines), raw.query, str(p)]
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
        "path": _to_workspace_relative(p),
        "query": raw.query,
        "context_lines": raw.context_lines,
        "hits": hits[:raw.max_results],
        "total_hits": len(hits[:raw.max_results]),
        "truncated": len(hits) >= raw.max_results,
    }


@register_tool("""
读取 workspace 内已知文本文件的指定行范围，返回内容和分页信息。

适用场景：
- 已经知道目标文件路径，需要阅读文件内容
- 根据 grep 命中的文件和行号，继续读取更完整的上下文
- 修改文件前，获取将要替换的真实原文片段

不适用场景：
- 不知道文件在哪里；先用 glob 查找路径
- 不知道关键词在哪个文件；先用 grep 定位
- 读取二进制文件、图片、压缩包等非文本内容

使用提示：
- offset 从 1 开始，用于指定读取起始行
- limit 控制最多读取多少行，但单次内容过长仍会被截断
- 返回 truncated=true 时，必须使用 next_offset 继续读取后续内容
- 修改文件前，old_block 应来自 read_file 或 grep 返回的真实原文
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