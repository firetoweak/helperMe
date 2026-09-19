from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from helperme.assistant.delivery import DELIVER_TOOL_NAME
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.sessions import SessionView, session_view
from helperme.assistant.subagent.subagent import project_parent, project_pending
from helperme.assistant.workspaces import bound_workspace_id
from helperme.runtime import (
    CommandPhase,
    CommandState,
    Event,
    OutcomeStatus,
    SqliteJournal,
    StepCommitted,
    StepState,
    UserMessageReceived,
    replay,
)


ToolStatus = Literal[
    "running",
    "succeeded",
    "failed",
    "unknown",
    "awaiting_authorization",
    "rejected",
]
SessionActivity = Literal["running", "idle"]
UNKNOWN_TOOL_ERROR = "执行中断，结果未知"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    workspace_id: str
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
    arguments: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepItem:
    kind: Literal["step"]
    step_id: str
    output_id: str
    text: str | None
    tools: tuple[ToolItem, ...]
    occurred_at: datetime
    thinking: str | None = None


ConversationItem = UserItem | StepItem


@dataclass(frozen=True, slots=True)
class ConversationView:
    session_id: str
    workspace_id: str | None
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
            workspace_id = bound_workspace_id(events)
            if workspace_id is None:
                continue
            summaries.append(
                project_session_summary(
                    session_id,
                    events,
                    workspace_id=workspace_id,
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
        state = replay(session_id, events).state
        if view is None:
            # 只读投影：control_approval 取 Host 持有的 Worker 镜像，
            # 不能改成 Host 请求，那会唤醒 Worker 并把读当成运行广播出去。
            view = session_view(
                state,
                has_active_subagents=bool(project_pending(events)),
                control_approval=self._sessions.control_approval(session_id),
                auto_authorize=self._sessions.web_auto_authorize(session_id),
                paused=self._sessions.is_paused(session_id),
            )
        return project_conversation(
            session_id,
            events,
            state.steps,
            session=view,
            activity=self._sessions.activity(session_id),
        )


def recent_workspace_id(summaries: tuple[SessionSummary, ...]) -> str | None:
    """最近一次聊天的工作区；列表已按 updated_at 降序，取第一条即可。"""
    return summaries[0].workspace_id if summaries else None


def project_session_summary(
    session_id: str,
    events: tuple[Event, ...],
    *,
    workspace_id: str,
    activity: SessionActivity,
) -> SessionSummary:
    title = "新会话"
    for event in events:
        if isinstance(event.payload, UserMessageReceived):
            title = event.payload.content.strip().splitlines()[0]
            break
    return SessionSummary(
        session_id=session_id,
        workspace_id=workspace_id,
        title=title,
        updated_at=events[-1].occurred_at if events else None,
        activity=activity,
    )


def project_conversation(
    session_id: str,
    events: tuple[Event, ...],
    steps: tuple[StepState, ...],
    *,
    session: SessionView,
    activity: SessionActivity = "idle",
) -> ConversationView:
    by_event = {step.committed_event_id: step for step in steps}
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
        step = by_event[event.event_id]
        text = step.step.decision.content.strip()
        thinking = _step_thinking(step.decision_metadata)
        tools: list[ToolItem] = []
        for command_state in step.commands:
            effect = command_state.command.effect
            if effect.name == DELIVER_TOOL_NAME:
                continue
            status, error = _tool_status(command_state, activity)
            tools.append(
                ToolItem(
                    command_state.command.command_id,
                    effect.name,
                    status,
                    error,
                    effect.argument_dict(),
                )
            )
        if text or tools or thinking:
            items.append(
                StepItem(
                    "step",
                    payload.step.step_id,
                    payload.step.trigger_event_id,
                    text or None,
                    tuple(tools),
                    event.occurred_at,
                    thinking,
                )
            )
    return ConversationView(
        session_id=session_id,
        workspace_id=bound_workspace_id(events),
        revision=events[-1].sequence if events else 0,
        items=tuple(items),
        session=session,
    )


def _step_thinking(metadata: object) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    extensions = metadata.get("message_extensions")
    if not isinstance(extensions, Mapping):
        return None
    text = extensions.get("reasoning_content")
    if type(text) is not str:
        return None
    stripped = text.strip()
    return stripped or None


def _tool_status(
    state: CommandState,
    activity: SessionActivity,
) -> tuple[ToolStatus, str | None]:
    """工具在时间线上的状态，除 unknown 外全部来自 Runtime 的确定事实。

    起过 attempt 却没有结果时，Journal 分不出「还在跑」和「已中断」——
    那是进程事实，只有 activity 知道。
    """

    outcome = state.outcome
    if outcome is not None:
        if outcome.status is OutcomeStatus.SUCCEEDED:
            return ("succeeded", None)
        return ("failed", outcome.error_message)
    if state.authorization_rejected_by_event_id is not None:
        return ("rejected", None)
    if state.phase is CommandPhase.UNKNOWN:
        if activity == "idle":
            return ("unknown", UNKNOWN_TOOL_ERROR)
        return ("running", None)
    if state.dispatch_eligible_by_event_id is None:
        return ("awaiting_authorization", None)
    return ("running", None)
