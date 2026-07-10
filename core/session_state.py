from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Enun


@dataclass
class Session:
    id: str
    conversation: Conversation
    status: SessionStatus = SessionStatus.PENDING
    events: list[SessionEvent] = field(default_factory=list)
    run_records: list[SessionRunRecord] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    facts: list[SessionFact] = field(default_factory=list)
    progress: SessionProgress = field(default_factory=SessionProgress)


@dataclass
class Conversation:
    pass

@dataclass
class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


ALLOWED_TRANSITIONS = {
    SessionStatus.PENDING: {SessionStatus.RUNNING},
    SessionStatus.RUNNING: {        
        SessionStatus.INTERRUPTED,
        SessionStatus.COMPLETED,
        SessionStatus.BLOCKED,
        SessionStatus.FAILED,
    },
    SessionStatus.INTERRUPTED: {SessionStatus.RUNNING},
    SessionStatus.COMPLETED: set(),
    SessionStatus.BLOCKED: set(),
    SessionStatus.FAILED: set(),
}

def transition_to(self, target: SessionStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[self.status]:
        raise InvalidSessionTransition(self.status, target)
    self.status = target