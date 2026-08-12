from __future__ import annotations

import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.tool_registry import EmptyInput, PydanticParameters, ToolSpec
from tools.workspace import (
    AbsolutePathNotAllowed,
    PathOutsideWorkspace,
    WorkspaceInputError,
    WorkspaceSandbox,
    WorkspaceSandboxes,
    workspace_error,
)


GET_WORKSPACE_INFO_DESCRIPTION = """
用途：列出当前可用 workspace root 的逻辑名称。
何时使用：不知道该选哪个 root 时使用；它用于发现工作区，不代替 glob/read_file 等文件操作工具。
关键限制：无参数，必须传 {}；物理路径是内部配置，其他 Workspace 工具必须使用返回的 root 名称和 root 内相对 path。
失败/截断后：结果不会截断；若缺少预期 root，应检查应用配置，不能猜测 root 名称或改用绝对 path 绕过。
""".strip()

GLOB_DESCRIPTION = """
用途：在指定 workspace root 中按名称模式查找文件或目录，返回可继续传给其他文件工具的相对路径。
何时使用：不知道目标文件位置、需要按扩展名或目录层级定位时使用；搜索文件内容用 grep，读取已知文件用 read_file。
关键限制：root 必须是已配置名称；path 和 pattern 必须相对 root，不能包含越界；glob 只定位名称，不读取文件内容，指向 root 外的符号链接结果会被排除。
失败/截断后：truncated=true 时使用 next_offset 继续，或缩小 path、pattern、kind、max_depth；GLOB_PARTIAL 与 complete=false 表示结果可用但搜索不完整，必须检查 inaccessible_paths，不能把无匹配当作完整结论；root/path 错误时先用 get_workspace_info 或修正相对路径。
""".strip()

GREP_DESCRIPTION = """
用途：在指定 workspace root 的文件或目录内按关键词或正则搜索文本，返回每个匹配行的位置和内容。
何时使用：不知道内容出现在哪个文件、修改前需要定位原文时使用；找文件名用 glob，阅读匹配位置的完整上下文用 read_file。
关键限制：root 必须是已配置名称，path 必须相对 root；query 按正则解释；一条 hit 表示一行匹配，正文和 submatches 都是有界预览，不能替代 read_file。
失败/截断后：truncated=true 时使用 next_offset 继续，或缩小 path/query；content_truncated=true 时用 read_file 查看该行；RG_TIMEOUT/RG_NOT_FOUND/RG_FAILED 时不能假定没有匹配。
""".strip()

READ_FILE_DESCRIPTION = """
用途：读取指定 workspace root 内已知文本文件的行范围，返回内容、行号和分页状态。
何时使用：已经知道文件路径、需要查看 grep 命中的完整上下文或在修改前取得真实 old_block 时使用；不知道路径先用 glob/grep，不要用它读取二进制文件。
关键限制：root 必须是已配置名称，path 必须相对 root；offset 从 1 开始；文件最大 20 MiB，单次最多 2000 行和 8000 字符，不能把局部结果当作完整文件。
失败/截断后：truncated=true 时使用 next_offset 继续；LINE_TOO_LONG 返回有界 preview 但不声称读取成功；FILE_TOO_LARGE 时先用 grep 定位或改用专用工具。
""".strip()


MAX_READ_FILE_SIZE_BYTES = 20 * 1024 * 1024
MAX_READ_CHARS = 8_000
MAX_GREP_HIT_CHARS = 2_000
MAX_GREP_PAGE_CHARS = 8_000
MAX_GREP_SUBMATCHES = 100
MAX_RG_ERROR_CHARS = 4_000
MAX_GLOB_INACCESSIBLE_PATHS = 100
GREP_TIMEOUT_SECONDS = 30
GREP_SHUTDOWN_TIMEOUT_SECONDS = 5


class RootInput(BaseModel):
    root: str = Field(description="workspace root 的逻辑名称；只能使用 get_workspace_info 返回的 name")


class GlobInput(RootInput):
    pattern: str = Field(description="glob 模式；不含 / 时递归匹配文件名，含 / 时匹配相对搜索起点的路径，例如 *.py、tools/*.py")
    path: str = Field(default=".", description="root 内的相对搜索起点；默认搜索整个 root")
    kind: Literal["file", "dir", "any"] = Field(default="any", description="结果类型：file 仅文件、dir 仅目录、any 两者都要")
    max_depth: int | None = Field(default=None, ge=1, description="相对搜索起点的深度限制；默认不限制，1 表示只看当前层")
    offset: int = Field(default=0, ge=0, description="跳过的匹配结果数量，从 0 开始")
    max_results: int = Field(default=10, ge=1, le=100, description="最多返回的结果数量，范围 1 到 100")


class ReadFileInput(RootInput):
    path: str = Field(description="root 内要读取的相对文件路径")
    offset: int = Field(default=1, ge=1, description="读取起始行号，从 1 开始")
    limit: int = Field(default=200, ge=1, le=2000, description="最多读取行数，范围 1 到 2000")


class GrepInput(RootInput):
    query: str = Field(description="搜索关键词或正则表达式")
    path: str = Field(default=".", description="root 内的相对搜索路径")
    offset: int = Field(default=0, ge=0, description="跳过的匹配行数量，从 0 开始")
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


def _walk_entries(
    root: Path,
    max_depth: int | None,
    inaccessible: list[Path],
    depth: int = 1,
):
    try:
        with os.scandir(root) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except PermissionError:
        if len(inaccessible) <= MAX_GLOB_INACCESSIBLE_PATHS:
            inaccessible.append(root)
        return
    for entry in entries:
        candidate = Path(entry.path)
        yield candidate
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except PermissionError:
            if len(inaccessible) <= MAX_GLOB_INACCESSIBLE_PATHS:
                inaccessible.append(candidate)
            continue
        if is_dir and (max_depth is None or depth < max_depth):
            yield from _walk_entries(
                candidate,
                max_depth,
                inaccessible,
                depth + 1,
            )


def _matches_glob(path: str, pattern: str) -> bool:
    candidate = PurePosixPath(path)
    if "/" not in pattern:
        return PurePosixPath(candidate.name).match(pattern)
    return PurePosixPath(f"/{path}").match(f"/{pattern}")


def create_file_read_specs(workspaces: WorkspaceSandboxes) -> list[ToolSpec]:
    async def get_workspace_info(_: EmptyInput) -> dict[str, Any]:
        return {
            "ok": True,
            "code": "WORKSPACE_INFO_READ",
            "roots": workspaces.info(),
        }

    async def glob(raw: GlobInput) -> dict[str, Any]:
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

        matches = []
        skipped = 0
        inaccessible: list[Path] = []
        for candidate in _walk_entries(
            search_root,
            raw.max_depth,
            inaccessible,
        ):
            try:
                workspace_path = candidate.relative_to(sandbox.root).as_posix()
                absolute_candidate = sandbox.resolve(workspace_path)
            except WorkspaceInputError:
                continue

            if not absolute_candidate.exists():
                continue
            candidate_kind = "dir" if absolute_candidate.is_dir() else "file"
            if raw.kind != "any" and candidate_kind != raw.kind:
                continue

            relative_search_path = candidate.relative_to(search_root).as_posix()
            if not _matches_glob(relative_search_path, pattern):
                continue

            if skipped < raw.offset:
                skipped += 1
                continue
            matches.append({"path": workspace_path, "kind": candidate_kind})
            if len(matches) > raw.max_results:
                break

        truncated = len(matches) > raw.max_results
        page = matches[:raw.max_results]
        inaccessible_paths = [
            path.relative_to(sandbox.root).as_posix()
            for path in inaccessible[:MAX_GLOB_INACCESSIBLE_PATHS]
        ]
        complete = not inaccessible
        return {
            "ok": True,
            "code": "GLOB_COMPLETED" if complete else "GLOB_PARTIAL",
            "root": raw.root,
            "pattern": raw.pattern,
            "path": raw.path,
            "matches": page,
            "complete": complete,
            "inaccessible_paths": inaccessible_paths,
            "inaccessible_paths_truncated": (
                len(inaccessible) > MAX_GLOB_INACCESSIBLE_PATHS
            ),
            "truncated": truncated,
            "next_offset": raw.offset + len(page) if truncated else None,
            "hint": (
                "结果不完整；检查 inaccessible_paths，并在需要完整结论时缩小 path。"
                if not complete
                else None
            ),
        }

    async def grep(raw: GrepInput) -> dict[str, Any]:
        if not raw.query.strip():
            return {"ok": False, "code": "EMPTY_QUERY", "error": "query 不能为空"}
        if shutil.which("rg") is None:
            return {"ok": False, "code": "RG_NOT_FOUND", "error": "未找到 rg"}
        try:
            sandbox = workspaces.get(raw.root)
            path, err = _require_existing(sandbox, raw.path, expect="any")
        except WorkspaceInputError as exc:
            return workspace_error(exc)
        if err:
            return err

        hits = []
        skipped = 0
        truncated = False
        page_chars = 0
        timed_out = threading.Event()
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                ["rg", "--json", "--sort", "path", "--", raw.query, str(path)],
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
            )

            def kill_on_timeout() -> None:
                if proc.poll() is None:
                    timed_out.set()
                    proc.kill()

            timer = threading.Timer(GREP_TIMEOUT_SECONDS, kill_on_timeout)
            timer.daemon = True
            timer.start()
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    if not raw_line.strip():
                        continue
                    obj = json.loads(raw_line)
                    if obj.get("type") != "match":
                        continue
                    data = obj.get("data") or {}
                    if skipped < raw.offset:
                        skipped += 1
                        continue
                    if len(hits) >= raw.max_results:
                        truncated = True
                        break

                    full_content = data["lines"]["text"].rstrip("\r\n")
                    content = full_content[:MAX_GREP_HIT_CHARS]
                    if hits and page_chars + len(content) > MAX_GREP_PAGE_CHARS:
                        truncated = True
                        break
                    submatches = data["submatches"]
                    hits.append({
                        "file": sandbox.relative(data["path"]["text"]),
                        "line": data["line_number"],
                        "content": content,
                        "content_truncated": len(content) < len(full_content),
                        "submatches": [
                            {"start": match["start"], "end": match["end"]}
                            for match in submatches[:MAX_GREP_SUBMATCHES]
                        ],
                        "submatches_truncated": len(submatches) > MAX_GREP_SUBMATCHES,
                    })
                    page_chars += len(content)

                if truncated and proc.poll() is None:
                    proc.terminate()
                try:
                    return_code = proc.wait(timeout=GREP_SHUTDOWN_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    return_code = proc.wait()
            finally:
                timer.cancel()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()

            stderr_file.seek(0)
            stderr = stderr_file.read(MAX_RG_ERROR_CHARS).strip()

        if timed_out.is_set():
            return {
                "ok": False,
                "code": "RG_TIMEOUT",
                "error": f"rg 搜索超过 {GREP_TIMEOUT_SECONDS} 秒",
            }
        if not truncated and return_code == 2:
            return {
                "ok": False,
                "code": "RG_FAILED",
                "error": stderr or "rg 执行失败",
            }

        return {
            "ok": True,
            "code": "GREP_COMPLETED",
            "root": raw.root,
            "path": sandbox.relative(path),
            "query": raw.query,
            "hits": hits,
            "truncated": truncated,
            "next_offset": raw.offset + len(hits) if truncated else None,
        }

    async def read_file(raw: ReadFileInput) -> dict[str, Any]:
        try:
            sandbox = workspaces.get(raw.root)
            path, err = _require_existing(sandbox, raw.path, expect="file")
        except WorkspaceInputError as exc:
            return workspace_error(exc)
        if err:
            return err
        relative_path = sandbox.relative(path)
        file_size = path.stat().st_size
        if file_size > MAX_READ_FILE_SIZE_BYTES:
            return {
                "ok": False,
                "code": "FILE_TOO_LARGE",
                "error": f"文件超过 20 MiB，不能使用 read_file: {relative_path}",
                "path": relative_path,
                "size": file_size,
                "max_size": MAX_READ_FILE_SIZE_BYTES,
            }

        selected: list[str] = []
        content_length = 0
        end_line = raw.offset - 1
        truncated_by = None
        next_offset = None
        last_seen_line = 0

        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    last_seen_line = line_number
                    if line_number < raw.offset:
                        continue
                    if len(selected) >= raw.limit:
                        truncated_by = "lines"
                        next_offset = line_number
                        break
                    if content_length + len(line) > MAX_READ_CHARS:
                        if selected:
                            truncated_by = "chars"
                            next_offset = line_number
                            break
                        return {
                            "ok": False,
                            "code": "LINE_TOO_LONG",
                            "error": f"第 {line_number} 行超过单次字符上限: {relative_path}",
                            "path": relative_path,
                            "line": line_number,
                            "preview": line[:MAX_READ_CHARS],
                            "max_chars": MAX_READ_CHARS,
                        }
                    selected.append(line)
                    content_length += len(line)
                    end_line = line_number
        except UnicodeDecodeError:
            return {
                "ok": False,
                "code": "NOT_A_TEXT_FILE",
                "error": f"无法以 UTF-8 文本读取: {relative_path}",
                "path": relative_path,
            }

        if not selected and last_seen_line < raw.offset:
            if not (raw.offset == 1 and last_seen_line == 0):
                return {
                    "ok": False,
                    "code": "OFFSET_OUT_OF_RANGE",
                    "error": f"起始行号超出文件末尾: {relative_path}",
                    "path": relative_path,
                }

        return {
            "ok": True,
            "code": "FILE_READ",
            "root": raw.root,
            "path": relative_path,
            "content": "".join(selected),
            "start_line": raw.offset,
            "end_line": end_line,
            "next_offset": next_offset,
            "truncated": truncated_by is not None,
            "truncated_by": truncated_by,
        }

    return [
        ToolSpec(
            name="get_workspace_info",
            description=GET_WORKSPACE_INFO_DESCRIPTION,
            parameters=PydanticParameters(EmptyInput),
            handler=get_workspace_info,
        ),
        ToolSpec(
            name="glob",
            description=GLOB_DESCRIPTION,
            parameters=PydanticParameters(GlobInput),
            handler=glob,
        ),
        ToolSpec(
            name="grep",
            description=GREP_DESCRIPTION,
            parameters=PydanticParameters(GrepInput),
            handler=grep,
        ),
        ToolSpec(
            name="read_file",
            description=READ_FILE_DESCRIPTION,
            parameters=PydanticParameters(ReadFileInput),
            handler=read_file,
        ),
    ]
