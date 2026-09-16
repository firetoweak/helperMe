from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from helperme.assistant.delivery import DELIVER_TOOL_NAME
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.sessions import SessionView, session_view
from helperme.assistant.subagent.subagent import project_parent, project_pending
from helperme.runtime import (
    CommandOutcomeReceived,
    DispatchAttemptStarted,
    Event,
    OutcomeStatus,
    SqliteJournal,
    StepCommitted,
    UserMessageReceived,
    replay,
)


ToolStatus = Literal["running", "succeeded", "failed", "unknown"]
SessionActivity = Literal["running", "idle"]
UNKNOWN_TOOL_ERROR = "执行中断，结果未知"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    updated_at: datetime | None
    activity: SessionActivity


@dataclass(frozen=True, slots=True)
class UserItem:
    kind: Literal["user"]
    message_id: str
    text: str
    occurred_at: datetime
    images: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolItem:
    command_id: str
    name: str
    status: ToolStatus
    error: str | None


@dataclass(frozen=True, slots=True)
class StepItem:
    kind: Literal["step"]
    step_id: str
    output_id: str
    text: str | None
    tools: tuple[ToolItem, ...]
    occurred_at: datetime


ConversationItem = UserItem | StepItem


@dataclass(frozen=True, slots=True)
class ConversationView:
    session_id: str
    revision: int
    items: tuple[ConversationItem, ...]
    session: SessionView


class AssistantQueries:
    """Read-only Assistant projections for native Channels."""

    def __init__(self, store: SessionStore, sessions) -> None:
        self._store = store
        self._sessions = sessions

    async def list_sessions(self) -> tuple[SessionSummary, ...]:
        summaries: list[SessionSummary] = []
        for path in self._store.journals():
            journal = SqliteJournal(path)
            session_id = await journal.session_identity()
            events = await journal.snapshot(session_id)
            if project_parent(events) is not None:
                continue
            if not any(
                isinstance(event.payload, UserMessageReceived) for event in events
            ):
                continue
            summaries.append(
                project_session_summary(
                    session_id,
                    events,
                    activity=self._sessions.activity(session_id),
                )
            )
        return tuple(
            sorted(
                summaries,
                key=lambda item: (
                    item.updated_at is not None,
                    item.updated_at or datetime.min,
                    item.session_id,
                ),
                reverse=True,
            )
        )

    async def conversation(
        self,
        session_id: str,
        *,
        view: SessionView | None = None,
    ) -> ConversationView:
        events = await SqliteJournal(
            self._store.require(session_id)
        ).snapshot(session_id)
        if view is None:
            view = session_view(
                replay(session_id, events).state,
                has_active_subagents=bool(project_pending(events)),
            )
        return project_conversation(
            session_id,
            events,
            session=view,
            activity=self._sessions.activity(session_id),
        )


def project_session_summary(
    session_id: str,
    events: tuple[Event, ...],
    *,
    activity: SessionActivity,
) -> SessionSummary:
    title = "新会话"
    for event in events:
        if isinstance(event.payload, UserMessageReceived):
            title = event.payload.content.strip().splitlines()[0]
            break
    return SessionSummary(
        session_id=session_id,
        title=title,
        updated_at=events[-1].occurred_at if events else None,
        activity=activity,
    )


def project_conversation(
    session_id: str,
    events: tuple[Event, ...],
    *,
    session: SessionView,
    activity: SessionActivity = "idle",
) -> ConversationView:
    outcomes = _command_outcomes(events)
    started = _started_commands(events)
    items: list[ConversationItem] = []
    for event in events:
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            items.append(
                UserItem(
                    "user",
                    event.event_id,
                    payload.content,
                    event.occurred_at,
                    event.artifact_refs,
                )
            )
            continue
        if not isinstance(payload, StepCommitted):
            continue
        text = payload.step.decision.content.strip()
        tools: list[ToolItem] = []
        for command in payload.step.commands:
            if command.effect.name == DELIVER_TOOL_NAME:
                continue
            recorded = outcomes.get(command.command_id)
            status, error = (
                recorded
                if recorded is not None
                else _open_tool_status(
                    command.command_id in started,
                    activity,
                )
            )
            tools.append(
                ToolItem(
                    command.command_id,
                    command.effect.name,
                    status,
                    error,
                )
            )
        if text or tools:
            items.append(
                StepItem(
                    "step",
                    payload.step.step_id,
                    payload.step.trigger_event_id,
                    text or None,
                    tuple(tools),
                    event.occurred_at,
                )
            )
    return ConversationView(
        session_id=session_id,
        revision=events[-1].sequence if events else 0,
        items=tuple(items),
        session=session,
    )


def _open_tool_status(
    attempt_started: bool,
    activity: SessionActivity,
) -> tuple[ToolStatus, str | None]:
    if attempt_started and activity == "idle":
        return ("unknown", UNKNOWN_TOOL_ERROR)
    return ("running", None)


def _started_commands(events: tuple[Event, ...]) -> frozenset[str]:
    started: set[str] = set()
    for event in events:
        payload = event.payload
        if isinstance(payload, DispatchAttemptStarted):
            started.add(payload.command_id)
    return frozenset(started)


def _command_outcomes(
    events: tuple[Event, ...],
) -> dict[str, tuple[ToolStatus, str | None]]:
    outcomes: dict[str, tuple[ToolStatus, str | None]] = {}
    for event in events:
        payload = event.payload
        if not isinstance(payload, CommandOutcomeReceived):
            continue
        if payload.outcome.status is OutcomeStatus.SUCCEEDED:
            outcomes[payload.command_id] = ("succeeded", None)
        else:
            outcomes[payload.command_id] = (
                "failed",
                payload.outcome.error_message,
            )
    return outcomes
