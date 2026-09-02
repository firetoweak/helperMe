from __future__ import annotations

from dataclasses import dataclass

from helperme.assistant.control import (
    AssistantControlPlane,
    ControlApprovalView,
)
from helperme.assistant.runner import (
    SessionScheduler,
    pending_authorization_ids,
    resume_session,
)
from helperme.assistant.toolsets import ToolSurface
from helperme.assistant.management import ManagementSurface
from helperme.assistant.subagent import SubAgentHost
from helperme.runtime import AgentRuntime, RuntimeStatus
from helperme.runtime.model import CanonicalState, CommandPhase


@dataclass(frozen=True, slots=True)
class SessionView:
    status: str
    waiting_for: tuple[str, ...]
    pending_authorization_ids: tuple[str, ...]
    terminal: bool
    should_wake: bool
    control_approval: ControlApprovalView | None = None
    control_message: str | None = None


def session_view(
    state: CanonicalState,
    *,
    control_approval: ControlApprovalView | None = None,
    control_message: str | None = None,
) -> SessionView:
    terminal = state.status in {
        RuntimeStatus.COMPLETED,
        RuntimeStatus.TERMINATED,
    }
    return SessionView(
        status=state.status.value,
        waiting_for=state.waiting_for,
        pending_authorization_ids=pending_authorization_ids(state),
        terminal=terminal,
        should_wake=(
            not terminal
            and (
                state.status is RuntimeStatus.RUNNABLE
                or any(
                    command.phase is CommandPhase.PENDING for command in state.commands
                )
            )
        ),
        control_approval=control_approval,
        control_message=control_message,
    )


class AssistantSessions:
    """给定 Session identity 后的 Assistant 应用操作。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        surface: ToolSurface,
        scheduler: SessionScheduler,
        *,
        control: AssistantControlPlane,
        management: ManagementSurface,
        subagents: SubAgentHost | None = None,
    ) -> None:
        self._runtime = runtime
        self._surface = surface
        self._scheduler = scheduler
        self._control = control
        self._management = management
        self._subagents = subagents

    def _view(
        self,
        state: CanonicalState,
        *,
        control_message: str | None = None,
    ) -> SessionView:
        return session_view(
            state,
            control_approval=self._control.pending_view(state.session_id),
            control_message=control_message,
        )

    async def create(self, session_id: str) -> SessionView:
        created = await self._runtime.create_session(session_id)
        if not created:
            raise ValueError(f"Session 已存在: {session_id}")
        return await self.view(session_id)

    async def resume(self, session_id: str) -> SessionView:
        state = await resume_session(
            self._runtime,
            self._surface,
            session_id,
            self._management,
        )
        if self._subagents is not None:
            await self._subagents.rehydrate(session_id)
        if self._view(state).should_wake:
            await self._scheduler.wake(session_id)
        return self._view(state)

    async def view(self, session_id: str) -> SessionView:
        return self._view(await self._runtime.state(session_id))

    async def resolve_control(
        self,
        session_id: str,
        *,
        approved: bool,
    ) -> str:
        return await self._control.resolve(session_id, approved=approved)

    async def receive_user_message(
        self,
        session_id: str,
        content: str,
        *,
        delivery_id: str,
        source: str = "user",
    ) -> None:
        await self._runtime.receive_user_message(
            session_id,
            content,
            delivery_id=delivery_id,
            source=source,
        )
        await self._scheduler.wake(session_id)

    async def resolve_authorizations(
        self,
        session_id: str,
        *,
        approved: bool,
    ) -> None:
        state = await self._runtime.state(session_id)
        for command_id in pending_authorization_ids(state):
            if approved:
                await self._runtime.grant_command(session_id, command_id)
            else:
                await self._runtime.reject_command(session_id, command_id)
        await self._scheduler.wake(session_id)
