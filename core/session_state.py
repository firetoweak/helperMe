from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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


@dataclass
class SessionEvent:
    kind: SessionEventType
    session_id: str
    reason: str

    run_id: str | None = None
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


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

EVENT_KIND_BY_TRANSITION = {
    (SessionStatus.PENDING, SessionStatus.RUNNING): SessionEventType.STARTED,
    (SessionStatus.RUNNING, SessionStatus.INTERRUPTED): SessionEventType.INTERRUPTED,
    (SessionStatus.INTERRUPTED, SessionStatus.RUNNING): SessionEventType.RESUMED,
    (SessionStatus.RUNNING, SessionStatus.COMPLETED): SessionEventType.COMPLETED,
    (SessionStatus.RUNNING, SessionStatus.BLOCKED): SessionEventType.BLOCKED,
    (SessionStatus.RUNNING, SessionStatus.FAILED): SessionEventType.FAILED,
}

NON_TRANSITION_EVENT_KINDS = {
    SessionEventType.CREATED,
}

@dataclass
class Session:
    id: str
    conversation: Conversation = field(default_factory=Conversation)
    status: SessionStatus = SessionStatus.PENDING
    events: list[SessionEvent] = field(default_factory=list)
    run_records: list[SessionRunRecord] = field(default_factory=list)

    def transition_to(
        self,
        target: SessionStatus,
        event: SessionEvent,
    ) -> None:
        current = self.status
        if target not in ALLOWED_TRANSITIONS[self.status]:
            raise InvalidSessionTransition(current, target)
        if event.session_id != self.id:
            raise ValueError(
                f"事件属于 {event.session_id}，不能记录到 {self.id}"
            )
        expected_event_kind = EVENT_KIND_BY_TRANSITION[(current, target)]
        if event.kind != expected_event_kind:
            raise ValueError(
                f"状态迁移 {current.value} -> {target.value} 需要事件 "
                f"{expected_event_kind.value}，不能使用 {event.kind.value}"
            )
        self.status = target
        self.events.append(event)

    def record_event(self, event: SessionEvent) -> None:
        if event.session_id != self.id:
            raise ValueError(
                f"事件属于 {event.session_id}，不能记录到 {self.id}"
            )
        if event.kind not in NON_TRANSITION_EVENT_KINDS:
            raise ValueError(
                f"状态事件 {event.kind.value} 必须通过 transition_to 记录"
            )
        self.events.append(event)
