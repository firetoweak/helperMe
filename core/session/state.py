from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone

from core.messages import Conversation
from core.context import ContextState
from core.environment import EnvironmentSelection
from core.tools_runtime.progressive_toolsets import SessionCapabilitySnapshot


class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


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

    turn_id: str | None = None
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class SessionTurnRecord:
    turn_id: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    final_reason: str | None = None

@dataclass
class Session:
    id: str
    default_environment_selection: EnvironmentSelection
    conversation: Conversation = field(default_factory=Conversation)
    context_state: ContextState = field(default_factory=ContextState)
    status: SessionStatus = SessionStatus.PENDING
    events: list[SessionEvent] = field(default_factory=list)
    turn_records: list[SessionTurnRecord] = field(default_factory=list)
    capability_snapshot: SessionCapabilitySnapshot | None = None
    pending_approval_id: str | None = None

    def transition_to(
        self,
        target: SessionStatus,
        event: SessionEvent,
    ) -> None:
        self.status = target
        self.events.append(event)

    def record_event(self, event: SessionEvent) -> None:
        self.events.append(event)
