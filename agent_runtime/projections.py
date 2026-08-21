from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from agent_runtime.events import (
    CommandOutcomeReceived,
    Event,
    UserInterruptReceived,
    UserMessageReceived,
)
from agent_runtime.model import CanonicalState, Step
from agent_runtime.state import StateProjector


@dataclass(frozen=True, slots=True)
class OutcomeView:
    event_id: str
    sequence: int
    payload: CommandOutcomeReceived


@dataclass(frozen=True, slots=True)
class InterruptView:
    event_id: str
    sequence: int
    reason: str | None


@dataclass(frozen=True, slots=True)
class UserMessageView:
    event_id: str
    sequence: int
    content: str


@dataclass(frozen=True, slots=True)
class TurnView:
    stream_id: str
    user_messages: tuple[UserMessageView, ...]
    interrupts: tuple[InterruptView, ...]
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
    stream_id: str
    entries: tuple[TraceEntry, ...]


@dataclass(frozen=True, slots=True)
class ArtifactResolution:
    refs: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass(frozen=True, slots=True)
class ReplayView:
    state: CanonicalState
    turn: TurnView
    trace: TraceView
    artifacts: ArtifactResolution


def diagnose_artifacts(
    events: tuple[Event, ...],
    available_refs: Collection[str] = (),
) -> ArtifactResolution:
    refs = tuple(dict.fromkeys(
        ref
        for event in events
        for ref in event.artifact_refs
    ))
    available = frozenset(available_refs)
    missing = tuple(ref for ref in refs if ref not in available)
    return ArtifactResolution(refs=refs, missing=missing)


def project_turn(
    stream_id: str,
    events: tuple[Event, ...],
    projector: StateProjector | None = None,
) -> TurnView:
    state = (projector or StateProjector()).project(
        stream_id,
        events,
    ).state
    return TurnView(
        stream_id=stream_id,
        user_messages=tuple(
            UserMessageView(
                event_id=event.event_id,
                sequence=event.sequence,
                content=event.payload.content,
            )
            for event in events
            if isinstance(event.payload, UserMessageReceived)
        ),
        interrupts=tuple(
            InterruptView(
                event_id=event.event_id,
                sequence=event.sequence,
                reason=event.payload.reason,
            )
            for event in events
            if isinstance(event.payload, UserInterruptReceived)
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
    stream_id: str,
    events: tuple[Event, ...],
) -> TraceView:
    return TraceView(
        stream_id=stream_id,
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
    stream_id: str,
    events: tuple[Event, ...],
    available_artifact_refs: Collection[str] = (),
) -> ReplayView:
    projector = StateProjector()
    return ReplayView(
        state=projector.project(stream_id, events).state,
        turn=project_turn(stream_id, events, projector),
        trace=project_trace(stream_id, events),
        artifacts=diagnose_artifacts(events, available_artifact_refs),
    )
