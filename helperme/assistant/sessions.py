from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

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
from helperme.assistant.subagent.subagent import SubAgentHost, project_pending
from helperme.runtime import AgentRuntime, RuntimeStatus
from helperme.runtime.model import CanonicalState, CommandPhase


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    """待授权命令的展示信息：用户确认前需要看到自己在批准什么。"""

    command_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SessionView:
    status: str
    waiting_for: tuple[str, ...]
    pending_authorization_ids: tuple[str, ...]
    should_wake: bool
    has_active_subagents: bool = False
    control_approval: ControlApprovalView | None = None
    control_message: str | None = None
    auto_authorize: bool = False
    paused: bool = False
    pending_authorization_commands: tuple[PendingAuthorization, ...] = ()


def session_view(
    state: CanonicalState,
    *,
    control_approval: ControlApprovalView | None = None,
    control_message: str | None = None,
    has_active_subagents: bool = False,
    auto_authorize: bool = False,
    paused: bool = False,
) -> SessionView:
    pending_ids = pending_authorization_ids(state)
    by_id = {
        command_state.command.command_id: command_state.command
        for command_state in state.commands
    }
    return SessionView(
        status=state.status.value,
        waiting_for=state.waiting_for,
        pending_authorization_ids=pending_ids,
        should_wake=(
            state.status is RuntimeStatus.RUNNABLE
            or any(
                command.phase is CommandPhase.PENDING for command in state.commands
            )
        ),
        has_active_subagents=has_active_subagents,
        control_approval=control_approval,
        control_message=control_message,
        auto_authorize=auto_authorize,
        paused=paused,
        pending_authorization_commands=tuple(
            PendingAuthorization(
                command_id=command_id,
                name=by_id[command_id].effect.name,
                arguments=by_id[command_id].effect.argument_dict(),
            )
            for command_id in pending_ids
            if command_id in by_id
        ),
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
        self._preference: dict[str, bool] = {}
        self._auto_grant: dict[str, bool] = {}

    def _view(
        self,
        state: CanonicalState,
        *,
        control_message: str | None = None,
        has_active_subagents: bool = False,
    ) -> SessionView:
        return session_view(
            state,
            control_approval=self._control.pending_view(state.session_id),
            control_message=control_message,
            has_active_subagents=has_active_subagents,
            auto_authorize=self._preference.get(state.session_id, False),
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
        pending_subagents: tuple[str, ...] = ()
        if self._subagents is not None:
            pending_subagents = await self._subagents.rehydrate(session_id)
        if self._subagents is not None and self._subagents.has_returned(session_id):
            return self._view(state)
        if self._view(state).should_wake:
            await self._scheduler.wake(session_id)
        elif self._subagents is not None:
            await self._subagents.on_quiesced(session_id, state)
        return self._view(
            state,
            has_active_subagents=bool(pending_subagents),
        )

    async def view(self, session_id: str) -> SessionView:
        state = await self._runtime.state(session_id)
        has_active_subagents = False
        if self._subagents is not None:
            has_active_subagents = bool(
                project_pending(await self._runtime.snapshot(session_id))
            )
        return self._view(
            state,
            has_active_subagents=has_active_subagents,
        )

    async def resolve_control(
        self,
        session_id: str,
        *,
        approved: bool,
    ) -> str:
        message = await self._control.resolve(session_id, approved=approved)
        # 与命令授权一致：审批结果必须唤醒本轮，否则 agent 不会继续。
        await self._scheduler.wake(session_id)
        return message

    async def receive_user_message(
        self,
        session_id: str,
        content: str,
        *,
        delivery_id: str,
        source: str = "user",
        artifact_refs: tuple[str, ...] = (),
    ) -> None:
        await self._runtime.receive_user_message(
            session_id,
            content,
            delivery_id=delivery_id,
            source=source,
            artifact_refs=tuple(artifact_refs),
        )
        await self._scheduler.wake(session_id)

    async def accept_input(
        self,
        session_id: str,
        content: str,
        *,
        delivery_id: str,
        source: str = "user",
        artifact_refs: tuple[str, ...] = (),
    ) -> SessionView:
        view = await self.view(session_id)
        answer = content.strip().lower()
        if view.control_approval is not None and answer in {"yes", "y", "no", "n"}:
            message = await self.resolve_control(
                session_id,
                approved=answer in {"yes", "y"},
            )
            return replace(await self.view(session_id), control_message=message)
        if view.pending_authorization_ids and answer in {"yes", "y", "no", "n"}:
            await self.resolve_authorizations(
                session_id,
                approved=answer in {"yes", "y"},
            )
            return await self.view(session_id)
        await self.receive_user_message(
            session_id,
            content,
            delivery_id=delivery_id,
            source=source,
            artifact_refs=tuple(artifact_refs),
        )
        return await self.view(session_id)

    async def resolve_authorization(
        self,
        session_id: str,
        command_id: str,
        *,
        approved: bool,
    ) -> None:
        if approved:
            await self._runtime.grant_command(session_id, command_id)
        else:
            await self._runtime.reject_command(session_id, command_id)
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

    def web_auto_authorize(self, session_id: str) -> bool:
        return self._preference.get(session_id, False)

    def is_auto_authorized(self, session_id: str) -> bool:
        return self._auto_grant.get(session_id, False)

    async def apply_authorization_policy(
        self,
        session_id: str,
        *,
        preference: bool,
        grant: bool,
    ) -> SessionView:
        self._preference[session_id] = bool(preference)
        self._auto_grant[session_id] = bool(grant)
        if grant:
            await self._grant_pending(session_id)
        return await self.view(session_id)

    async def _grant_pending(self, session_id: str) -> None:
        state = await self._runtime.state(session_id)
        pending = pending_authorization_ids(state)
        if not pending:
            return
        for command_id in pending:
            await self._runtime.grant_command(session_id, command_id)
        await self._scheduler.wake(session_id)

    async def cancel_turn(self, session_id: str) -> SessionView:
        await self._scheduler.cancel_turn(session_id)
        return await self.view(session_id)
