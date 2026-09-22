from __future__ import annotations

import json
from pathlib import Path


class AutoAuthorizeStore:
    """Host-only Session 总闸。只在人拨过时写入，缺省 false。不进 Journal。"""

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
