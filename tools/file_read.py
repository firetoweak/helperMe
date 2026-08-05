from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.tool_registry import EmptyInput, ToolSpec
from tools.workspace import (
    AbsolutePathNotAllowed,
    PathOutsideWorkspace,
    WorkspaceInputError,
    WorkspaceSandbox,
    WorkspaceSandboxes,
    workspace_error,
)


GET_WORKSPACE_INFO_DESCRIPTION = """
用途：列出当前可用 workspace root 的逻辑名称、物理位置和系统平台。
何时使用：不知道该选哪个 root、用户询问文件位置或需要平台信息时使用；它用于发现工作区，不代替 glob/read_file 等文件操作工具。
关键限制：无参数，必须传 {}；返回的绝对位置仅用于说明，其他 Workspace 工具仍必须使用 root 名称和 root 内相对 path。
失败/截断后：结果不会截断；若缺少预期 root，应检查应用配置，不能猜测 root 名称或改用绝对 path 绕过。
""".strip()

GLOB_DESCRIPTION = """
用途：在指定 workspace root 中按名称模式查找文件或目录，返回可继续传给其他文件工具的相对路径。
何时使用：不知道目标文件位置、需要按扩展名或目录层级定位时使用；搜索文件内容用 grep，读取已知文件用 read_file。
关键限制：root 必须是已配置名称；path 和 pattern 必须相对 root，不能包含越界；glob 只定位名称，不读取文件内容，指向 root 外的符号链接结果会被排除。
失败/截断后：truncated=true 时缩小 path、pattern、kind、max_depth 或提高 max_results 后继续；无结果时调整搜索范围；root/path 错误时先用 get_workspace_info 或修正相对路径。
""".strip()

GREP_DESCRIPTION = """
用途：在指定 workspace root 的文件或目录内按关键词或正则搜索文本，返回命中位置和少量上下文。
何时使用：不知道内容出现在哪个文件、修改前需要定位原文时使用；找文件名用 glob，阅读已知文件的完整上下文用 read_file。
关键限制：root 必须是已配置名称，path 必须相对 root；query 按正则解释；结果仅用于定位，不能替代修改前的完整原文读取。
失败/截断后：truncated=true 时缩小 path/query 或提高 max_results 后继续；命中后用 read_file 读取目标区域；RG_NOT_FOUND/RG_FAILED 时修复搜索后端，不要假定没有匹配。
""".strip()

READ_FILE_DESCRIPTION = """
用途：读取指定 workspace root 内已知文本文件的行范围，返回内容、行号和分页状态。
何时使用：已经知道文件路径、需要查看 grep 命中的完整上下文或在修改前取得真实 old_block 时使用；不知道路径先用 glob/grep，不要用它读取二进制文件。
关键限制：root 必须是已配置名称，path 必须相对 root；offset 从 1 开始；单次最多受 limit 和字符预算共同限制，不能把局部结果当作完整文件。
失败/截断后：truncated=true 时必须使用 next_offset 继续，truncated_by 表示行数或字符限制；NOT_FOUND 后重新定位路径，OFFSET_OUT_OF_RANGE 后依据 total_lines 修正，NOT_A_TEXT_FILE 时改用适合该格式的工具。
""".strip()


class RootInput(BaseModel):
    root: str = Field(description="workspace root 的逻辑名称；只能使用 get_workspace_info 返回的 name")


class GlobInput(RootInput):
    pattern: str = Field(description="文件名或相对 path 的 glob 模式，例如 file_read.py、*.py、tools/*.py")
    path: str = Field(default=".", description="root 内的相对搜索起点；默认搜索整个 root")
    kind: Literal["file", "dir", "any"] = Field(default="any", description="结果类型：file 仅文件、dir 仅目录、any 两者都要")
    max_depth: int | None = Field(default=None, ge=1, description="相对搜索起点的深度限制；默认不限制，1 表示只看当前层")
    max_results: int = Field(default=10, ge=1, le=100, description="最多返回的结果数量，范围 1 到 100")


class ReadFileInput(RootInput):
    path: str = Field(description="root 内要读取的相对文件路径")
    offset: int = Field(default=1, ge=1, description="读取起始行号，从 1 开始")
    limit: int = Field(default=200, ge=1, le=2000, description="最多读取行数，范围 1 到 2000")


class GrepInput(RootInput):
    query: str = Field(description="搜索关键词或正则表达式")
    path: str = Field(default=".", description="root 内的相对搜索路径")
    context_lines: int = Field(default=2, ge=0, le=20, description="每个匹配前后各返回的上下文行数，范围 0 到 20")
    max_results: int = Field(default=10, ge=1, le=100, description="最多返回的匹配数量，范围 1 到 100")


def _require_existing(
    sandbox: WorkspaceSandbox,
    path: str,
    *,
    expect: Literal["file", "dir", "any"],
) -> tuple[Path | None, dict[str, Any] | None]:
    resolved = sandbox.resolve(path)
    if not resolved.exists():
        return None, {"ok": False, "code": "NOT_FOUND", "error": f"路径不存在: {path}"}
    if expect == "file" and not resolved.is_file():
        return None, {"ok": False, "code": "NOT_A_FILE", "error": f"不是文件: {path}"}
    if expect == "dir" and not resolved.is_dir():
        return None, {"ok": False, "code": "NOT_A_DIR", "error": f"不是目录: {path}"}
    return resolved, None


def create_file_read_specs(workspaces: WorkspaceSandboxes) -> list[ToolSpec]:
    def get_workspace_info(_: EmptyInput) -> dict[str, Any]:
        return {
            "ok": True,
            "code": "WORKSPACE_INFO_READ",
            "roots": workspaces.info(),
            "platform": sys.platform,
        }

    def glob(raw: GlobInput) -> dict[str, Any]:
        try:
            sandbox = workspaces.get(raw.root)
            search_root, err = _require_existing(sandbox, raw.path, expect="dir")
            if err:
                return err

            pattern = raw.pattern.replace("\\", "/")
            pattern_path = Path(pattern)
            if pattern_path.is_absolute() or pattern_path.drive:
                raise AbsolutePathNotAllowed(raw.pattern)
            if ".." in pattern_path.parts:
                raise PathOutsideWorkspace(raw.pattern)
        except WorkspaceInputError as exc:
            return workspace_error(exc)

        if not pattern.strip():
            return {"ok": False, "error": "pattern 不能为空", "code": "EMPTY_PATTERN"}

        has_path_part = "/" in pattern
        candidates = search_root.glob(pattern) if has_path_part else search_root.rglob(pattern)
        matches = []
        for candidate in candidates:
            try:
                relative_candidate = candidate.relative_to(sandbox.root).as_posix()
                absolute_candidate = sandbox.resolve(relative_candidate)
            except WorkspaceInputError:
                continue
            if raw.kind == "dir" and not absolute_candidate.is_dir():
                continue
            if raw.kind == "file" and not absolute_candidate.is_file():
                continue

            rel_from_search_root = absolute_candidate.relative_to(search_root)
            if raw.max_depth is not None and len(rel_from_search_root.parts) > raw.max_depth:
                continue
            matches.append({
                "path": sandbox.relative(absolute_candidate),
                "kind": "dir" if absolute_candidate.is_dir() else "file",
            })

        matches.sort(key=lambda item: item["path"])
        total = len(matches)
        return {
            "ok": True,
            "code": "GLOB_COMPLETED",
            "root": raw.root,
            "pattern": raw.pattern,
            "path": raw.path,
            "matches": matches[:raw.max_results],
            "total": total,
            "truncated": total > raw.max_results,
        }

    def grep(raw: GrepInput) -> dict[str, Any]:
        if shutil.which("rg") is None:
            return {"ok": False, "code": "RG_NOT_FOUND", "error": "未找到 rg"}
        try:
            sandbox = workspaces.get(raw.root)
            path, err = _require_existing(sandbox, raw.path, expect="any")
        except WorkspaceInputError as exc:
            return workspace_error(exc)
        if err:
            return err

        proc = subprocess.run(
            ["rg", "--json", "-C", str(raw.context_lines), raw.query, str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode == 2:
            return {"ok": False, "code": "RG_FAILED", "error": proc.stderr.strip() or "rg 执行失败"}

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
                if event_type == "match" and match_line is None:
                    match_line = line_number
                snippet.append({
                    "line": line_number,
                    "content": data["lines"]["text"].rstrip("\r\n"),
                    "kind": event_type,
                })
            elif event_type == "end" and snippet and match_line is not None:
                hits.append({
                    "file": sandbox.relative(current_file),
                    "line": match_line,
                    "snippet": snippet,
                })
                if len(hits) >= raw.max_results:
                    break

        return {
            "ok": True,
            "code": "GREP_COMPLETED",
            "root": raw.root,
            "path": sandbox.relative(path),
            "query": raw.query,
            "context_lines": raw.context_lines,
            "hits": hits,
            "total_hits": len(hits),
            "truncated": len(hits) >= raw.max_results,
        }

    def read_file(raw: ReadFileInput) -> dict[str, Any]:
        try:
            sandbox = workspaces.get(raw.root)
            path, err = _require_existing(sandbox, raw.path, expect="file")
        except WorkspaceInputError as exc:
            return workspace_error(exc)
        if err:
            return err
        relative_path = sandbox.relative(path)
        if raw.offset < 1:
            return {"ok": False, "code": "INVALID_OFFSET", "error": "offset 必须大于等于 1", "path": relative_path}
        if raw.limit < 1:
            return {"ok": False, "code": "INVALID_LIMIT", "error": "limit 必须大于等于 1", "path": relative_path}

        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return {"ok": False, "code": "NOT_A_TEXT_FILE", "error": f"无法以文本读取: {relative_path}"}
        total_lines = len(lines)
        if raw.offset > total_lines:
            return {
                "ok": False,
                "code": "OFFSET_OUT_OF_RANGE",
                "error": f"起始行号超出文件总行数: {relative_path}",
                "total_lines": total_lines,
                "path": relative_path,
            }

        selected = []
        content_length = 0
        stopped_by_chars = False
        for line in lines[raw.offset - 1:]:
            if len(selected) >= raw.limit:
                break
            if content_length + len(line) > 3000 and selected:
                stopped_by_chars = True
                break
            selected.append(line)
            content_length += len(line)

        end_line = raw.offset - 1 + len(selected)
        truncated_by = "chars" if stopped_by_chars else "lines" if end_line < total_lines else None
        return {
            "ok": True,
            "code": "FILE_READ",
            "root": raw.root,
            "path": relative_path,
            "content": "".join(selected),
            "start_line": raw.offset,
            "end_line": end_line,
            "total_lines": total_lines,
            "next_offset": end_line + 1 if end_line < total_lines else None,
            "truncated": truncated_by is not None,
            "truncated_by": truncated_by,
        }

    return [
        ToolSpec(
            name="get_workspace_info",
            description=GET_WORKSPACE_INFO_DESCRIPTION,
            input_model=EmptyInput,
            handler=get_workspace_info,
        ),
        ToolSpec(
            name="glob",
            description=GLOB_DESCRIPTION,
            input_model=GlobInput,
            handler=glob,
        ),
        ToolSpec(
            name="grep",
            description=GREP_DESCRIPTION,
            input_model=GrepInput,
            handler=grep,
        ),
        ToolSpec(
            name="read_file",
            description=READ_FILE_DESCRIPTION,
            input_model=ReadFileInput,
            handler=read_file,
        ),
    ]
