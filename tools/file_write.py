from tools.workspace import _resolve_in_workspace, _to_workspace_relative
from pydantic import BaseModel, Field
from core.tool_registry import register_tool
from typing import Any
import unicodedata


# 模糊匹配的文本标准化
def normalize_for_match(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFKC", text)
    return text

class ApplyPatchInput(BaseModel):
    path: str = Field(description="要修改的文本文件路径，相对worksplace的路径")
    old_block: str = Field(description="必须来自文件原文的精确文本块，且只能匹配一个位置")
    new_block: str = Field(description="替换后的文本块")

class ReplaceAllInput(BaseModel):
    path: str = Field(description="要修改的文本文件路径，相对 workspace")
    old_block: str = Field(description="要被全文替换的精确文本块")
    new_block: str = Field(description="替换后的文本块")


@register_tool("""
在 workspace 内对单个文本文件做一次局部替换。

适用场景：
- 只想修改文件中的一个明确位置
- 修改前已经通过 read_file 或 grep 拿到原文片段
- 需要保留文件其他内容不变

不适用场景：
- 新建文件或整体覆盖；用 write_file
- 同一个文本要全部替换；用 replace_all
- old_block 只是自然语言描述，而不是文件原文

使用提示：
- old_block 必须精确且唯一匹配
- 如果提示 old_block 不存在，应重新 read_file 获取最新原文
- 如果提示匹配不唯一，应扩大 old_block 的上下文后重试
- fuzzy candidate 只表示找到候选，不表示已经修改成功
""", input_model=ApplyPatchInput)
def apply_patch(raw: ApplyPatchInput) -> dict[str, Any]:

    p, err = _resolve_in_workspace(raw.path, expect="file")
    if err:
        return {"ok":False, **err}

    if not raw.old_block:
        return {"ok": False, "code": "OLD_BLOCK_EMPTY", "path": _to_workspace_relative(p)}

    try:
        content = p.read_text(encoding="utf-8")
        count = content.count(raw.old_block)
        if count == 1:
            new_content = content.replace(raw.old_block, raw.new_block, 1)
            try:
                p.write_text(new_content, encoding="utf-8")
            except OSError as e:
                return {
                    "ok": False, 
                    "code": "WRITE_FAILED",
                    "error": str(e),
                    "path": _to_workspace_relative(p),
                }
            return {
                "ok": True,
                "code": "PATCH_APPLIED",
                "path": _to_workspace_relative(p),
                "replacements": 1,
            }
        if count > 1:
            return {
                "ok": False,
                "code": "OLD_BLOCK_NOT_UNIQUE",
                "matches": count,
                "path": _to_workspace_relative(p),
            }
        # 模糊匹配
        old_lines = raw.old_block.splitlines(keepends=True)
        window_size = len(old_lines)
        norm_old = normalize_for_match(raw.old_block)

        content_lines = content.splitlines(keepends=True)
        candidates = []
        for i in range(0, len(content_lines) - window_size + 1):
            original_block = "".join(content_lines[i:i + window_size])
            if normalize_for_match(original_block) == norm_old:
                candidates.append({
                    "original_block": original_block,
                    "path": _to_workspace_relative(p),
                })
        if len(candidates) == 1:
            return {
                "ok": False,
                "code": "FUZZY_MATCH_ONLY",
                "message": "old_block 未精确匹配，但找到一个候选。请用 original_block 重新调用 apply_patch。",
                "candidate": candidates[0],
                "path": _to_workspace_relative(p),
            }
        if len(candidates) > 1:
            return {
                "ok": False,
                "code": "FUZZY_MATCH_NOT_UNIQUE",
                "matches": len(candidates),
                "candidates": candidates[:3],
                "path": _to_workspace_relative(p),
            }
        return {
            "ok": False,
            "code": "OLD_BLOCK_NOT_FOUND",
            "path": _to_workspace_relative(p),
        }
    except UnicodeDecodeError:
        return {"ok": False, "error": f"无法以文本读取（可能是二进制文件）: {p.as_posix()}", "code": "NOT_A_TEXT_FILE"}
    except OSError as e:
        return {"ok": False, "error": f"文件系统错误: {e}", "code": "FILE_SYSTEM_ERROR"}
    


@register_tool("""
在 workspace 内对单个文本文件执行全文批量替换。

适用场景：
- 明确希望所有相同 old_block 都被替换
- 统一术语、名称、格式或固定字符串

不适用场景：
- 只改一个位置；用 apply_patch
- 不确定是否所有出现位置都该改；先用 grep 检查
- 新建文件或整体覆盖；用 write_file

使用提示：
- old_block 必须是精确文本，不支持模糊匹配
- 调用前建议用 grep 确认出现位置和数量
""", input_model=ReplaceAllInput)
def replace_all(raw: ReplaceAllInput) -> dict[str, Any]:
    p, err = _resolve_in_workspace(raw.path, expect="file")
    if err:
        return {"ok": False, **err}
    if not raw.old_block:
        return {
            "ok": False,
            "code": "OLD_BLOCK_EMPTY",
            "path": _to_workspace_relative(p),
        }
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {
            "ok": False,
            "code": "NOT_A_TEXT_FILE",
            "error": f"无法以文本读取（可能是二进制文件）: {p.as_posix()}",
            "path": _to_workspace_relative(p),
        }
    count = content.count(raw.old_block)
    if count == 0:
        return {
            "ok": False,
            "code": "OLD_BLOCK_NOT_FOUND",
            "path": _to_workspace_relative(p),
            "replacements": 0,
        }
    new_content = content.replace(raw.old_block, raw.new_block)
    try:
        p.write_text(new_content, encoding="utf-8")
    except OSError as e:
        return {
            "ok": False,
            "code": "WRITE_FAILED",
            "error": str(e),
            "path": _to_workspace_relative(p),
        }
    return {
        "ok": True,
        "code": "REPLACE_ALL_APPLIED",
        "path": _to_workspace_relative(p),
        "replacements": count,
    }