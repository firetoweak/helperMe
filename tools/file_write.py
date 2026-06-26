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
    path: str = Field(description="文件路径，相对worksplace的路径")
    old_block: str = Field(description="要替换的原文块，必须在文件中精确唯一匹配")
    new_block: str = Field(description="替换后的部分")

class ReplaceAllInput(BaseModel):
    path: str = Field(description="文件路径，相对worksplace的路径")
    old_block: str = Field(description="要被替换的原文块，精确匹配，出现几次就替换几次")
    new_block: str = Field(description="替换后的文本块")


@register_tool("""
在 workspace 内对单个文本文件做局部替换。
只有当 old_block 在文件中精确且唯一匹配时，才会执行修改。
如果精确匹配失败，会尝试 newline+NFKC 归一化查找候选，但不会自动修改，只返回 candidate.original_block 供下一轮重试。
本工具不支持批量替换；如需全部替换，应使用 replace_all。

适用场景：
- 修正文档中的笔误、错误或过时信息
- 替换特定句子或段落
- 微调内容，更新具体细节

不适用场景：
- 全文件重写 → 用 write_file
- 搜索文件内容 → 用 grep
- 找文件路径 → 用 glob
- 批量替换所有相同字符串 → 用 replace_all
- old_block 不是原文片段，而是自然语言描述

输入：
  path       要修改的文件路径，相对 workspace 或绝对路径
  old_block  要替换的原文块，必须来自文件原文，并且精确唯一匹配
  new_block  替换后的新文本块

输出：
  ok            是否修改成功
  code          结果码
  path          修改文件路径
  replacements  替换次数，成功时固定为 1
  candidate     精确匹配失败但归一化后找到候选时返回，包含 original_block

结果码：
  PATCH_APPLIED            修改成功
  OLD_BLOCK_NOT_FOUND      old_block 未找到，应重新 read_file 获取原文
  OLD_BLOCK_NOT_UNIQUE     old_block 出现多次，应扩大上下文后重试
  FUZZY_MATCH_ONLY         归一化后找到唯一候选，但未修改；应使用 candidate.original_block 重新调用
  FUZZY_MATCH_NOT_UNIQUE   归一化后找到多个候选，应读取更大上下文后重试
  WRITE_FAILED             写入失败

重要规则：
- 不要编造 old_block；old_block 应来自 read_file 或 grep 返回的原文。
- 收到 FUZZY_MATCH_ONLY 时，不要认为修改成功，必须用 candidate.original_block 再调用一次。
- 收到 OLD_BLOCK_NOT_UNIQUE 时，不要随机选择一个位置，必须扩展上下文让 old_block 唯一。
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
在 workspace 内对单个文本文件做【全文批量】字符串替换。
会把文件中所有与 old_block 精确匹配的内容都替换成 new_block。
⚠️ 仅当你明确希望【所有出现位置】都修改时使用本工具。
只改一处请用 apply_patch。
适用场景：
- 全文统一修改术语、人名或地名
- 批量替换特定词汇
- 统一格式或规范

不适用场景：
- 只改一处 → 用 apply_patch
- old_block 在文件中出现 0 次
- 新建文件、模糊匹配、不确定是否该全改
输入：
  path       要修改的文件路径，相对 workspace
  old_block  要被替换的原文块（精确匹配）
  new_block  替换后的文本块
输出：
  ok            是否修改成功
  code          结果码
  path          文件路径（相对 workspace）
  replacements  实际替换次数
结果码：
  REPLACE_ALL_APPLIED    替换成功，replacements >= 1
  OLD_BLOCK_NOT_FOUND    old_block 未出现，未修改
  OLD_BLOCK_EMPTY        old_block 为空
  WRITE_FAILED           写入失败
  （以及 workspace 透传：OUT_OF_WORKSPACE, NOT_FOUND, NOT_A_FILE 等）
重要规则：
- 调用前建议先用 grep 确认 old_block 出现次数和位置。
- 不要用 apply_patch 处理「多处相同且都要改」的场景；那是 replace_all 的职责。
- 本工具不做模糊匹配；old_block 必须与磁盘原文完全一致。
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