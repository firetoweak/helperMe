from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from helperme.runtime.events import (
    CommandOutcomeReceived,
    Event,
    UserMessageReceived,
)
from helperme.runtime.model import CanonicalState, Step
from helperme.runtime.state import StateProjector


@dataclass(frozen=True, slots=True)
class OutcomeView:
    event_id: str
    sequence: int
    payload: CommandOutcomeReceived


@dataclass(frozen=True, slots=True)
class UserMessageView:
    event_id: str
    sequence: int
    content: str


@dataclass(frozen=True, slots=True)
class TurnView:
    session_id: str
    user_messages: tuple[UserMessageView, ...]
    steps: tuple[Step, ...]
    outcomes: tuple[OutcomeView, ...]


@dataclass(frozen=True, slots=True)
class TraceEntry:
    sequence: int
    event_id: str
    kind: str
    causation_id: str | None


@dataclass(frozen=True, slots=True)
class TraceView:
    session_id: str
    entries: tuple[TraceEntry, ...]


@dataclass(frozen=True, slots=True)
class ArtifactResolution:
    refs: tuple[str, ...]
    missing: tuple[str, ...]
    inspected: bool = True

    @property
    def complete(self) -> bool:
        if not self.inspected:
            return not self.refs
        return not self.missing


@dataclass(frozen=True, slots=True)
class ReplayView:
    state: CanonicalState
    turn: TurnView
    trace: TraceView
    artifacts: ArtifactResolution


def diagnose_artifacts(
    events: tuple[Event, ...],
    available_refs: Collection[str] | None = None,
) -> ArtifactResolution:
    refs = tuple(dict.fromkeys(ref for event in events for ref in event.artifact_refs))
    if available_refs is None:
        return ArtifactResolution(refs=refs, missing=(), inspected=False)
    available = frozenset(available_refs)
    missing = tuple(ref for ref in refs if ref not in available)
    return ArtifactResolution(refs=refs, missing=missing, inspected=True)


def project_turn(
    session_id: str,
    events: tuple[Event, ...],
    projector: StateProjector | None = None,
) -> TurnView:
    state = (
        (StateProjector() if projector is None else projector)
        .project(
            session_id,
            events,
        )
        .state
    )
    return TurnView(
        session_id=session_id,
        user_messages=tuple(
            UserMessageView(
                event_id=event.event_id,
                sequence=event.sequence,
                content=event.payload.content,
            )
            for event in events
            if isinstance(event.payload, UserMessageReceived)
        ),
        steps=state.steps,
        outcomes=tuple(
            OutcomeView(
                event_id=event.event_id,
                sequence=event.sequence,
                payload=event.payload,
            )
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
        ),
    )


def project_trace(
    session_id: str,
    events: tuple[Event, ...],
) -> TraceView:
    return TraceView(
        session_id=session_id,
        entries=tuple(
            TraceEntry(
                sequence=event.sequence,
                event_id=event.event_id,
                kind=event.kind,
                causation_id=event.causation_id,
            )
            for event in events
        ),
    )


def replay(
    session_id: str,
    events: tuple[Event, ...],
    available_artifact_refs: Collection[str] | None = None,
) -> ReplayView:
    projector = StateProjector()
    return ReplayView(
        state=projector.project(session_id, events).state,
        turn=project_turn(session_id, events, projector),
        trace=project_trace(session_id, events),
        artifacts=diagnose_artifacts(events, available_artifact_refs),
    )
