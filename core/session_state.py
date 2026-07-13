from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from datetime import datetime, timezone

from core.messages import Conversation


class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class InvalidSessionTransition(ValueError):
    def __init__(
        self,
        current: SessionStatus,
        target: SessionStatus,
        ) -> None:
        super().__init__(
            f"非法的 Session 状态转换：{current.value} -> {target.value}"
        )
        self.current = current
        self.target = target


# transition_to state
class SessionEventType(str, Enum):
    CREATED = "session_created"
    STARTED = "session_started"
    INTERRUPTED = "session_interrupted"
    RESUMED = "session_resumed"
    COMPLETED = "session_completed"
    BLOCKED = "session_blocked"
    FAILED = "session_failed"
    HUMAN_FEEDBACK_ADDED = "human_feedback_added"
    RUNTIME_FEEDBACK_ADDED = "runtime_feedback_added"


class SessionEventSource(str, Enum):
    RUNTIME = "runtime"
    HUMAN = "human"


@dataclass
class SessionEvent:
    kind: SessionEventType
    session_id: str
    source: SessionEventSource
    reason: str

    run_id: str | None = None
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionRunRecord:
    run_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    final_reason: str | None = None

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


@dataclass
class Session:
    id: str
    conversation: Conversation = field(default_factory=Conversation)
    status: SessionStatus = SessionStatus.PENDING
    events: list[SessionEvent] = field(default_factory=list)
    run_records: list[SessionRunRecord] = field(default_factory=list)

    def transition_to(self, target: SessionStatus) -> None:
        if target not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidSessionTransition(self.status, target)
        self.status = target

    def record_event(self, event: SessionEvent) -> None:
        if event.session_id != self.id:
            raise ValueError(
                f"事件属于 {event.session_id}，不能记录到 {self.id}"
            )

        self.events.append(event)

    # facts: list[SessionFact] = field(default_factory=list)
    # progress: SessionProgress = field(default_factory=SessionProgress)
