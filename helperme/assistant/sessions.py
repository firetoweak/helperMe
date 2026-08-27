from __future__ import annotations

from dataclasses import dataclass

from helperme.assistant.completion.judgment import JudgmentPolicy
from helperme.assistant.control import (
    AssistantControlPlane,
    ControlApprovalView,
)
from helperme.assistant.runner import (
    SessionNotFoundError,
    drive_until_idle,
    pending_authorization_ids,
    resume_session,
)
from helperme.assistant.toolsets import ToolSurface
from helperme.assistant.management import ManagementSurface
from helperme.runtime import AgentRuntime, RuntimeStatus
from helperme.runtime.model import CanonicalState


@dataclass(frozen=True, slots=True)
class SessionView:
    status: str
    waiting_for: tuple[str, ...]
    pending_authorization_ids: tuple[str, ...]
    terminal: bool
    should_drive: bool
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
        should_drive=(
            not terminal
            and (
                state.status is RuntimeStatus.RUNNABLE
                or bool(state.waiting_command_ids)
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
        *,
        policy: JudgmentPolicy | None = None,
        control: AssistantControlPlane | None = None,
        management: ManagementSurface | None = None,
    ) -> None:
        self._runtime = runtime
        self._surface = surface
        self._policy = policy
        self._control = control
        self._management = management

    def _view(
        self,
        state: CanonicalState,
        *,
        control_message: str | None = None,
    ) -> SessionView:
        return session_view(
            state,
            control_approval=(
                None
                if self._control is None
                else self._control.pending_view(state.session_id)
            ),
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
        return self._view(state)

    async def view(self, session_id: str) -> SessionView:
        return self._view(await self._runtime.state(session_id))

    async def resolve_control(
        self,
        session_id: str,
        *,
        approved: bool,
    ) -> str:
        if self._control is None:
            raise ValueError("Assistant 未装配对话控制面")
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
        if self._policy is not None:
            await self._policy.on_user_message(
                self._runtime,
                session_id,
                content,
            )

    async def receive_interrupt(
        self,
        session_id: str,
        reason: str,
        *,
        delivery_id: str,
        source: str = "user",
    ) -> None:
        await self._runtime.receive_interrupt(
            session_id,
            reason,
            delivery_id=delivery_id,
            source=source,
        )
        await self._runtime.advance(session_id)

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

    async def drive(
        self,
        session_id: str,
    ) -> SessionView:
        result = await drive_until_idle(
            self._runtime,
            session_id,
            policy=self._policy,
            control=self._control,
        )
        return self._view(
            result.state,
            control_message=result.control_message,
        )
