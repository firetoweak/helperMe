from __future__ import annotations

import unicodedata
from typing import Any

from pydantic import BaseModel, Field

from core.tool_registry import ToolSpec
from tools.workspace import (
    WorkspaceInputError,
    WorkspaceSandboxes,
    workspace_error,
)


APPLY_PATCH_DESCRIPTION = """
用途：在指定 workspace root 内对单个文本文件执行一次精确且唯一的局部替换，保留其他内容不变。
何时使用：已通过 read_file 或 grep 取得真实原文、只需修改一个明确位置时使用；新建或整体覆盖用 write_file，所有相同文本都要替换时用 replace_all。
关键限制：root 必须是已配置名称，path 必须相对 root；old_block 必须来自最新文件原文并且唯一匹配；模糊候选只用于提示，不会自动写入。
失败/截断后：OLD_BLOCK_NOT_FOUND 后重新 read_file；OLD_BLOCK_NOT_UNIQUE 后扩大上下文；FUZZY_MATCH_ONLY 时使用返回的 original_block 明确重试；本工具结果不截断。
""".strip()

REPLACE_ALL_DESCRIPTION = """
用途：在指定 workspace root 内把单个文本文件中所有精确匹配的 old_block 批量替换为 new_block。
何时使用：明确希望统一术语、名称或固定字符串的全部出现位置时使用；只改一个位置用 apply_patch，不确定影响范围时先用 grep。
关键限制：root 必须是已配置名称，path 必须相对 root；会修改全部精确匹配，且不支持模糊匹配；调用前应确认每个命中都应该被修改。
失败/截断后：OLD_BLOCK_NOT_FOUND 后用 grep/read_file 获取最新原文和数量，再决定是否重试；结果不截断，成功后用 get_changes 核对实际改动。
""".strip()


def normalize_for_match(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFKC", text)


class ApplyPatchInput(BaseModel):
    root: str = Field(description="workspace root 的逻辑名称；只能使用 get_workspace_info 返回的 name")
    path: str = Field(description="root 内要修改的相对文本文件路径")
    old_block: str = Field(description="必须来自文件原文的精确文本块，且只能匹配一个位置")
    new_block: str = Field(description="替换后的文本块")


class ReplaceAllInput(BaseModel):
    root: str = Field(description="workspace root 的逻辑名称；只能使用 get_workspace_info 返回的 name")
    path: str = Field(description="root 内要修改的相对文本文件路径")
    old_block: str = Field(description="要被全文替换的精确文本块")
    new_block: str = Field(description="替换后的文本块")


def _existing_file(workspaces: WorkspaceSandboxes, root: str, path: str):
    sandbox = workspaces.get(root)
    resolved = sandbox.resolve(path)
    if not resolved.exists():
        return sandbox, None, {"ok": False, "code": "NOT_FOUND", "error": f"路径不存在: {path}"}
    if not resolved.is_file():
        return sandbox, None, {"ok": False, "code": "NOT_A_FILE", "error": f"不是文件: {path}"}
    return sandbox, resolved, None


def create_file_write_specs(workspaces: WorkspaceSandboxes) -> list[ToolSpec]:
    def apply_patch(raw: ApplyPatchInput) -> dict[str, Any]:
        try:
            sandbox, path, err = _existing_file(workspaces, raw.root, raw.path)
        except WorkspaceInputError as exc:
            return workspace_error(exc)
        if err:
            return err
        relative_path = sandbox.relative(path)
        if not raw.old_block:
            return {"ok": False, "code": "OLD_BLOCK_EMPTY", "path": relative_path}

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "code": "NOT_A_TEXT_FILE", "error": f"无法以文本读取: {relative_path}"}

        count = content.count(raw.old_block)
        if count == 1:
            path.write_text(content.replace(raw.old_block, raw.new_block, 1), encoding="utf-8")
            return {"ok": True, "code": "PATCH_APPLIED", "root": raw.root, "path": relative_path, "replacements": 1}
        if count > 1:
            return {"ok": False, "code": "OLD_BLOCK_NOT_UNIQUE", "matches": count, "path": relative_path}

        old_lines = raw.old_block.splitlines(keepends=True)
        window_size = len(old_lines)
        norm_old = normalize_for_match(raw.old_block)
        content_lines = content.splitlines(keepends=True)
        candidates = []
        for index in range(0, len(content_lines) - window_size + 1):
            original_block = "".join(content_lines[index:index + window_size])
            if normalize_for_match(original_block) == norm_old:
                candidates.append({"original_block": original_block, "path": relative_path})
        if len(candidates) == 1:
            return {
                "ok": False,
                "code": "FUZZY_MATCH_ONLY",
                "hint": "old_block 未精确匹配，但找到一个候选。请用 original_block 重试。",
                "candidate": candidates[0],
                "path": relative_path,
            }
        if len(candidates) > 1:
            return {"ok": False, "code": "FUZZY_MATCH_NOT_UNIQUE", "matches": len(candidates), "candidates": candidates[:3], "path": relative_path}
        return {"ok": False, "code": "OLD_BLOCK_NOT_FOUND", "path": relative_path}

    def replace_all(raw: ReplaceAllInput) -> dict[str, Any]:
        try:
            sandbox, path, err = _existing_file(workspaces, raw.root, raw.path)
        except WorkspaceInputError as exc:
            return workspace_error(exc)
        if err:
            return err
        relative_path = sandbox.relative(path)
        if not raw.old_block:
            return {"ok": False, "code": "OLD_BLOCK_EMPTY", "path": relative_path}
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "code": "NOT_A_TEXT_FILE", "error": f"无法以文本读取: {relative_path}"}
        count = content.count(raw.old_block)
        if count == 0:
            return {"ok": False, "code": "OLD_BLOCK_NOT_FOUND", "path": relative_path, "replacements": 0}
        path.write_text(content.replace(raw.old_block, raw.new_block), encoding="utf-8")
        return {"ok": True, "code": "REPLACE_ALL_APPLIED", "root": raw.root, "path": relative_path, "replacements": count}

    return [
        ToolSpec(
            name="apply_patch",
            description=APPLY_PATCH_DESCRIPTION,
            input_model=ApplyPatchInput,
            handler=apply_patch,
        ),
        ToolSpec(
            name="replace_all",
            description=REPLACE_ALL_DESCRIPTION,
            input_model=ReplaceAllInput,
            handler=replace_all,
        ),
    ]
