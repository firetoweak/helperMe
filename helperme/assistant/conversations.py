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
    Event,
    OutcomeStatus,
    SqliteJournal,
    StepCommitted,
    UserMessageReceived,
    replay,
)


ToolStatus = Literal["running", "succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    updated_at: datetime | None
    activity: Literal["running", "idle"]


@dataclass(frozen=True, slots=True)
class UserItem:
    kind: Literal["user"]
    message_id: str
    text: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class AssistantItem:
    kind: Literal["assistant"]
    message_id: str
    output_id: str
    text: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ToolItem:
    kind: Literal["tool"]
    command_id: str
    name: str
    status: ToolStatus
    occurred_at: datetime
    error: str | None


ConversationItem = UserItem | AssistantItem | ToolItem


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
        return project_conversation(session_id, events, session=view)


def project_session_summary(
    session_id: str,
    events: tuple[Event, ...],
    *,
    activity: Literal["running", "idle"],
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
) -> ConversationView:
    outcomes = _command_outcomes(events)
    items: list[ConversationItem] = []
    for event in events:
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            items.append(
                UserItem("user", event.event_id, payload.content, event.occurred_at)
            )
            continue
        if not isinstance(payload, StepCommitted):
            continue
        text = payload.step.decision.content.strip()
        if text:
            items.append(
                AssistantItem(
                    "assistant",
                    event.event_id,
                    payload.step.trigger_event_id,
                    text,
                    event.occurred_at,
                )
            )
        for command in payload.step.commands:
            if command.effect.name == DELIVER_TOOL_NAME:
                continue
            status, error = outcomes.get(command.command_id, ("running", None))
            items.append(
                ToolItem(
                    "tool",
                    command.command_id,
                    command.effect.name,
                    status,
                    event.occurred_at,
                    error,
                )
            )
    return ConversationView(
        session_id=session_id,
        revision=events[-1].sequence if events else 0,
        items=tuple(items),
        session=session,
    )


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
