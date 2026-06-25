from pathlib import Path
from typing import Literal, Any
Expect = Literal["file", "dir", "any"]

# 简单的worksplace
WORKSPACE = Path(__file__).resolve().parent.parent


def _to_workspace_relative(path: str) -> str:
    p = Path(path).resolve()
    try:
        return p.relative_to(WORKSPACE).as_posix()
    except ValueError:
        return p.as_posix()

def _resolve_in_workspace(
    path: str,
    *,
    must_exist: bool = True,
    expect: Expect = "any", # 期望的类型，file, dir, any
    create_parents: bool = False
) -> tuple[Path | None, dict[str, Any] | None]:
    """
    将外部传入路径解析为 workspace 内的安全路径。

    作用：
    - 支持相对路径，默认相对 WORKSPACE
    - 规范化路径，消除 . / ..
    - 阻止路径逃逸到 WORKSPACE 外
    - 可选检查存在性和文件类型

    成功: (resolved_path, None)
    失败: (None, {"error": "...", "code": "..."})
    """
    ws = WORKSPACE.resolve()

    # 1. 解析路径 是workplace下的绝对路径
    p = Path(path)
    if not p.is_absolute():
        p = ws / p
  
    try:
        p = p.resolve()
        p.relative_to(ws)
    except OSError as e:
        return None, {"error": f"路径无法解析: {path}, {e}", "code": "RESOLVE_FAILED_OS_ERROR"}
    except ValueError:
        return None, {"error": f"路径越界 workspace: {p.as_posix()}", "code": "OUT_OF_WORKSPACE"}

    if not must_exist:
        parent = p.parent
        if not create_parents :
            if not parent.exists():
                return None, {"error": f"父目录不存在: {parent.as_posix()}", "code": "PARENT_NOT_FOUND"}
            if not parent.is_dir():
                return None, {"error": f"父路径不是目录: {parent.as_posix()}", "code": "PARENT_NOT_DIR"}
            return p, None
        else:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return None, {"error": f"无法创建父目录: {e}", "code": "MKDIR_FAILED"}
            return p, None

    if not p.exists():
        return None, {"error": f"路径不存在: {p.as_posix()}", "code": "NOT_FOUND"}

    if expect == "file" and not p.is_file():
        return None, {"error": f"不是文件: {p.as_posix()}", "code": "NOT_A_FILE"}
    if expect == "dir" and not p.is_dir():
        return None, {"error": f"不是目录: {p.as_posix()}", "code": "NOT_A_DIR"}

    return p, None