from __future__ import annotations

import json
from pathlib import Path


class AutoAuthorizeStore:
    """Host-only Web 总闸。只在人拨过时写入，缺省 false。不进 Journal。"""

    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._values: dict[str, bool] = {}
        self._load()

    def get(self, session_id: str) -> bool:
        return self._values.get(session_id, False)

    def remember(self, session_id: str, enabled: bool) -> None:
        self._values[session_id] = bool(enabled)

    def set(self, session_id: str, enabled: bool) -> None:
        self.remember(session_id, enabled)
        self._save()

    def _load(self) -> None:
        if self._root is None:
            return
        path = self._root / "auto_authorize.json"
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError("auto_authorize.json must be an object")
        self._values = {str(key): bool(value) for key, value in raw.items()}

    def _save(self) -> None:
        if self._root is None:
            return
        path = self._root / "auto_authorize.json"
        path.write_text(
            json.dumps(self._values, ensure_ascii=False),
            encoding="utf-8",
        )


def auto_grant_for_owners(owners: tuple[str, ...], preference: bool) -> bool:
    # 入口策略：verdict=ask 的命令是否自动放行由当前 owner 决定。
    # Web：看 Session 总闸偏好，没拨过就是关。
    if any(owner.startswith("web:") for owner in owners):
        return preference
    # TUI：不自动放行，等待 yes/no。ExecPolicy 是防误操作的软边界，
    # 误操作在任何入口都是误操作。
    if "tui" in owners:
        return False
    # ACP / Telegram 等暂无授权交互入口的 Channel：保持放行，
    # 待各自入口实现授权交互后接入。
    if owners:
        return True
    # 没有 owner：不替人放行。
    return False
