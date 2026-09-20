from __future__ import annotations

import json
from pathlib import Path


class SessionPauseStore:
    """Host-only session hold. Only written when a person toggles it. Not in the Journal."""

    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._values: dict[str, bool] = {}
        self._load()

    def get(self, session_id: str) -> bool:
        return self._values.get(session_id, False)

    def remember(self, session_id: str, paused: bool) -> None:
        self._values[session_id] = bool(paused)

    def set(self, session_id: str, paused: bool) -> None:
        self.remember(session_id, paused)
        self._save()

    def _load(self) -> None:
        if self._root is None:
            return
        path = self._root / "paused.json"
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            raise ValueError("paused.json must be an object")
        self._values = {str(key): bool(value) for key, value in raw.items()}

    def _save(self) -> None:
        if self._root is None:
            return
        path = self._root / "paused.json"
        path.write_text(
            json.dumps(self._values, ensure_ascii=False),
            encoding="utf-8",
        )
