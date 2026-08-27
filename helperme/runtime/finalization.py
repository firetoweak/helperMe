from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from datetime import datetime, timezone

from helperme.runtime.events import (
    Event,
    EventDraft,
    RuntimeCompleted,
    RuntimeTerminated,
    StepCommitted,
    TerminationRequested,
    UserMessageReceived,
)
from helperme.runtime.model import (
    CommandPhase,
    InvokeTool,
    LifecycleIntent,
    RuntimeStatus,
)
from helperme.runtime.state import StateProjector


class FinalizationKind(str, Enum):
    COMPLETE = "complete"
    TERMINATE_FROM_STEP = "terminate_from_step"
    TERMINATE_FROM_REQUEST = "terminate_from_request"


@dataclass(frozen=True, slots=True)
class FinalizationOpportunity:
    kind: FinalizationKind
    declared_by_event_id: str
    abandoned_command_ids: tuple[str, ...] = ()


def terminal_status(events: tuple[Event, ...]) -> RuntimeStatus | None:
    for event in events:
        payload = event.payload
        if isinstance(payload, RuntimeCompleted):
            return RuntimeStatus.COMPLETED
        if isinstance(payload, RuntimeTerminated):
            return RuntimeStatus.TERMINATED
    return None


def finalization_opportunity(
    session_id: str,
    events: tuple[Event, ...],
) -> FinalizationOpportunity | None:
    if terminal_status(events) is not None:
        return None
    request = _live_termination_request(events)
    projection = StateProjector().project(session_id, events)
    if request is not None:
        abandoned = tuple(
            state.command.command_id
            for state in projection.state.commands
            if state.phase is not CommandPhase.TERMINAL and not state.abandoned
        )
        return FinalizationOpportunity(
            kind=FinalizationKind.TERMINATE_FROM_REQUEST,
            declared_by_event_id=request.event_id,
            abandoned_command_ids=abandoned,
        )
    if projection.next_decision is not None:
        return None
    if not projection.state.steps:
        return None
    latest = projection.state.steps[-1]
    intent = latest.decision.lifecycle_intent
    if intent is LifecycleIntent.NONE:
        return None
    if _necessary_command_ids(projection.state.commands):
        return None
    declared_by = _step_event_id(events, latest.step_id)
    if intent is LifecycleIntent.COMPLETE:
        return FinalizationOpportunity(
            kind=FinalizationKind.COMPLETE,
            declared_by_event_id=declared_by,
        )
    return FinalizationOpportunity(
        kind=FinalizationKind.TERMINATE_FROM_STEP,
        declared_by_event_id=declared_by,
    )


def runtime_completed_payload(
    opportunity: FinalizationOpportunity,
) -> RuntimeCompleted:
    if opportunity.kind is not FinalizationKind.COMPLETE:
        raise ValueError("opportunity is not a completion")
    return RuntimeCompleted(opportunity.declared_by_event_id)


def runtime_terminated_payload(
    opportunity: FinalizationOpportunity,
) -> RuntimeTerminated:
    if opportunity.kind is FinalizationKind.COMPLETE:
        raise ValueError("opportunity is not a termination")
    return RuntimeTerminated(
        opportunity.declared_by_event_id,
        abandoned_command_ids=opportunity.abandoned_command_ids,
    )


def terminal_event_draft(
    session_id: str,
    event_id: str,
    opportunity: FinalizationOpportunity,
) -> EventDraft:
    payload = (
        runtime_completed_payload(opportunity)
        if opportunity.kind is FinalizationKind.COMPLETE
        else runtime_terminated_payload(opportunity)
    )
    return EventDraft(
        event_id=event_id,
        session_id=session_id,
        payload=payload,
        occurred_at=datetime.now(timezone.utc),
        causation_id=opportunity.declared_by_event_id,
    )


def _necessary_command_ids(commands) -> tuple[str, ...]:
    return tuple(
        state.command.command_id
        for state in commands
        if isinstance(state.command.effect, InvokeTool)
        and state.phase is not CommandPhase.TERMINAL
        and not state.abandoned
        and state.authorization_rejected_by_event_id is None
    )


def _live_termination_request(events: tuple[Event, ...]) -> Event | None:
    latest: Event | None = None
    for event in events:
        payload = event.payload
        if isinstance(payload, TerminationRequested):
            latest = event
            continue
        if latest is None:
            continue
        if isinstance(
            payload,
            (UserMessageReceived, StepCommitted),
        ):
            latest = None
    return latest


def _step_event_id(events: tuple[Event, ...], step_id: str) -> str:
    for event in events:
        payload = event.payload
        if isinstance(payload, StepCommitted) and payload.step.step_id == step_id:
            return event.event_id
    raise KeyError(step_id)
