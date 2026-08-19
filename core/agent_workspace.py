from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentWorkspace:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @classmethod
    def default(cls) -> "AgentWorkspace":
        return cls(Path.home() / ".helperme")

    @property
    def sessions_root(self) -> Path:
        return self.root / "sessions"

    @property
    def plugins_root(self) -> Path:
        return self.root / "plugins"

    @property
    def skills_root(self) -> Path:
        return self.root / "skills"

    @property
    def state_root(self) -> Path:
        return self.root / "state"

    def initialize(self) -> None:
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.plugins_root.mkdir(parents=True, exist_ok=True)
        self.skills_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
