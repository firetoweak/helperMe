from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Callable

from core.environment import EnvironmentProvider, EnvironmentSelection

from core.tools_runtime.turn_runtime import (
    TurnControl,
    TurnResult,
    TurnRuntime,
    TurnStatus,
)
from core.tools_runtime.turn_invocation import TurnInvocation
from core.tools_runtime.progressive_toolsets import (
    SessionCapabilitySnapshot,
    SnapshotToolsetProvider,
    ToolsetProvider,
)
from core.approval import ApprovalRequest
from core.session.state import (
    Session,
    SessionEvent,
    SessionEventType,
    SessionTurnRecord,
    SessionStatus,
)
from datetime import datetime, timezone

MAX_USER_MESSAGE_CHARS = 32_000

TURN_STATUS_MAPPING = {
    TurnStatus.COMPLETED: (
        SessionStatus.COMPLETED,
        SessionEventType.COMPLETED,
    ),
    TurnStatus.INTERRUPTED: (
        SessionStatus.INTERRUPTED,
        SessionEventType.INTERRUPTED,
    ),
    TurnStatus.BLOCKED: (
        SessionStatus.BLOCKED,
        SessionEventType.BLOCKED,
    ),
    TurnStatus.FAILED: (
        SessionStatus.FAILED,
        SessionEventType.FAILED,
    ),
}


@dataclass(frozen=True)
class SessionTurnOutcome:
    record: SessionTurnRecord
    result: TurnResult



class SessionRuntime:
    def __init__(
        self,
        turn_runtime: TurnRuntime | None = None,
        *,
        environment_provider: EnvironmentProvider,
        default_environment_selection: EnvironmentSelection,
        turn_runtime_factory: Callable[[str], TurnRuntime] | None = None,
        delete_session_resources: Callable[[str], None] | None = None,
        default_toolset_provider: ToolsetProvider | None = None,
    ):
        if (turn_runtime is None) == (turn_runtime_factory is None):
            raise ValueError(
                "turn_runtime 与 turn_runtime_factory 必须且只能提供一个"
            )
        if turn_runtime_factory is not None and delete_session_resources is None:
            raise ValueError(
                "使用 turn_runtime_factory 时必须提供 delete_session_resources"
            )
        self.turn_runtime = turn_runtime
        self._turn_runtime_factory = turn_runtime_factory
        self._delete_session_resources = delete_session_resources
        self._session_turn_runtimes: dict[str, TurnRuntime] = {}
        self.sessions: dict[str, Session] = {}
        self.active_controls: dict[str, TurnControl] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self.default_toolset_provider = default_toolset_provider
        self.environment_provider = environment_provider
        self.default_environment_selection = default_environment_selection

    def create_session(
        self,
        session_id: str,
        system_prompt: str,
    ) -> Session:
        if not session_id or not session_id.strip():
            raise ValueError("session_id 不能为空")
        if session_id in self.sessions:
            raise ValueError(f"重复 session_id: {session_id}")
        if not system_prompt or not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")

        session = Session(
            id=session_id,
            default_environment_selection=self.default_environment_selection,
            capability_snapshot=(
                SessionCapabilitySnapshot.capture(
                    self.default_toolset_provider
                )
                if self.default_toolset_provider is not None
                else None
            ),
        )
        session.conversation.set_system_prompt(system_prompt)
        event = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id=session.id,
            reason="Session created",
        )

        session.record_event(event)
        if self._turn_runtime_factory is not None:
            self._session_turn_runtimes[session.id] = self._turn_runtime_factory(
                session.id
            )
        self._turn_locks[session.id] = asyncio.Lock()
        self.sessions[session.id] = session
        return session

    def delete_session(self, session_id: str) -> None:
        if not session_id or not session_id.strip():
            raise ValueError("session_id 不能为空")
        if session_id not in self.sessions:
            raise KeyError(f"Session 不存在: {session_id}")
        if (
            session_id in self.active_controls
            or self._turn_locks[session_id].locked()
        ):
            raise ValueError(f"不能删除正在执行的 Session: {session_id}")

        if self._delete_session_resources is not None:
            self._delete_session_resources(session_id)
        self._session_turn_runtimes.pop(session_id, None)
        del self._turn_locks[session_id]
        del self.sessions[session_id]

    def get_session(self, session_id: str) -> Session:
        return self.sessions[session_id]

    async def start(
        self,
        session_id: str,
        turn_id: str,
        user_message: str,
        max_steps: int = 20,
        *,
        invocation: TurnInvocation | None = None,
    ) -> SessionTurnOutcome:
        if not session_id or not session_id.strip():
            raise ValueError("session_id 不能为空")
        self.validate_turn_input(turn_id, user_message)

        if session_id not in self.sessions:
            raise KeyError(f"Session 不存在: {session_id}")

        session = self.sessions[session_id]
        if session.pending_approval_id is not None:
            raise ValueError(
                "Session 正在等待审批，必须先输入 yes 或 no"
            )
        if session.status not in {
            SessionStatus.PENDING,
            SessionStatus.COMPLETED,
            SessionStatus.BLOCKED,
            SessionStatus.FAILED,
        }:
            raise ValueError(
                "Session 状态必须允许启动新 Turn，"
                f"当前为: {session.status.value}"
            )

        return await self._begin_and_execute_turn(
            session=session,
            turn_id=turn_id,
            user_message=user_message,
            max_steps=max_steps,
            event_kind=SessionEventType.STARTED,
            event_reason="Session started",
            invocation=invocation,
        )


    def request_interrupt(
        self,
        session_id: str,
        reason: str | None = None,
    ) -> None:
        if not session_id or not session_id.strip():
            raise ValueError("session_id 不能为空")
        if session_id not in self.sessions:
            raise KeyError(f"Session 不存在: {session_id}")

        session = self.sessions[session_id]
        if session.status != SessionStatus.RUNNING:
            raise ValueError(
                f"Session 状态必须为 running，当前为: {session.status.value}"
            )
        if session_id not in self.active_controls:
            raise RuntimeError(
                f"运行中的 Session 缺少 active control: {session_id}"
            )
        control = self.active_controls[session_id]
        control.request_interrupt(reason)


    async def resume(
        self,
        session_id: str,
        turn_id: str,
        user_message: str,
        max_steps: int = 20,
        *,
        invocation: TurnInvocation | None = None,
    ) -> SessionTurnOutcome:
        if not session_id or not session_id.strip():
            raise ValueError("session_id 不能为空")
        self.validate_turn_input(turn_id, user_message)
        if session_id not in self.sessions:
            raise KeyError(f"Session 不存在: {session_id}")

        session = self.sessions[session_id]
        if session.status != SessionStatus.INTERRUPTED:
            raise ValueError(
                f"Session 状态必须为 interrupted，当前为: {session.status.value}"
            )

        return await self._begin_and_execute_turn(
            session=session,
            turn_id=turn_id,
            user_message=user_message,
            max_steps=max_steps,
            event_kind=SessionEventType.RESUMED,
            event_reason="Session resumed",
            invocation=invocation,
        )

    @staticmethod
    def validate_turn_input(turn_id: str, user_message: str) -> None:
        if not turn_id or not turn_id.strip():
            raise ValueError("turn_id 不能为空")
        SessionRuntime._validate_user_message(user_message)

    @staticmethod
    def _validate_user_message(user_message: str) -> None:
        if not user_message or not user_message.strip():
            raise ValueError("user_message 不能为空")
        if len(user_message) > MAX_USER_MESSAGE_CHARS:
            raise ValueError(
                "user_message 超过单次输入上限: "
                f"{len(user_message)} > {MAX_USER_MESSAGE_CHARS}"
            )

    async def _begin_and_execute_turn(
        self,
        session: Session,
        turn_id: str,
        user_message: str,
        max_steps: int,
        event_kind: SessionEventType,
        event_reason: str,
        invocation: TurnInvocation | None,
    ) -> SessionTurnOutcome:
        async with self._turn_locks[session.id]:
            return await self._execute_serialized_turn(
                session,
                turn_id,
                user_message,
                max_steps,
                event_kind,
                event_reason,
                invocation,
            )

    async def _execute_serialized_turn(
        self,
        session: Session,
        turn_id: str,
        user_message: str,
        max_steps: int,
        event_kind: SessionEventType,
        event_reason: str,
        invocation: TurnInvocation | None,
    ) -> SessionTurnOutcome:
        if session.id in self.active_controls:
            raise ValueError(f"Session 已有正在执行的 Turn: {session.id}")
        allowed_statuses = (
            {SessionStatus.INTERRUPTED}
            if event_kind is SessionEventType.RESUMED
            else {
                SessionStatus.PENDING,
                SessionStatus.COMPLETED,
                SessionStatus.BLOCKED,
                SessionStatus.FAILED,
            }
        )
        if session.status not in allowed_statuses:
            raise ValueError(
                f"Session 当前状态不能进入该 Turn: {session.status.value}"
            )
        if any(record.turn_id == turn_id for record in session.turn_records):
            raise ValueError(f"重复 turn_id: {turn_id}")

        effective_invocation = await self._resolve_environment_invocation(
            session,
            invocation or TurnInvocation(),
        )

        turn_control = TurnControl()
        turn_record = SessionTurnRecord(
            turn_id=turn_id,
            status="running",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            final_reason=None,
        )
        event = SessionEvent(
            kind=event_kind,
            session_id=session.id,
            reason=event_reason,
            turn_id=turn_id,
        )
        session.transition_to(SessionStatus.RUNNING, event)
        session.turn_records.append(turn_record)
        self.active_controls[session.id] = turn_control

        try:
            return await self._execute_turn(
                session=session,
                turn_record=turn_record,
                user_message=user_message,
                max_steps=max_steps,
                control=turn_control,
                invocation=effective_invocation,
            )
        except asyncio.CancelledError:
            if session.status is SessionStatus.RUNNING:
                ended_at = datetime.now(timezone.utc)
                event = SessionEvent(
                    kind=SessionEventType.FAILED,
                    session_id=session.id,
                    reason="Turn task was cancelled",
                    turn_id=turn_id,
                )
                session.transition_to(SessionStatus.FAILED, event)
                turn_record.status = TurnStatus.FAILED.value
                turn_record.ended_at = ended_at
                turn_record.final_reason = "task_cancelled"
            raise
        except Exception:
            if session.status is SessionStatus.RUNNING:
                ended_at = datetime.now(timezone.utc)
                event = SessionEvent(
                    kind=SessionEventType.FAILED,
                    session_id=session.id,
                    reason="TurnRuntime raised an exception",
                    turn_id=turn_id,
                )
                session.transition_to(SessionStatus.FAILED, event)
                turn_record.status = TurnStatus.FAILED.value
                turn_record.ended_at = ended_at
                turn_record.final_reason = "runtime_exception"
            raise
        finally:
            del self.active_controls[session.id]

    async def _resolve_environment_invocation(
        self,
        session: Session,
        invocation: TurnInvocation,
    ) -> TurnInvocation:
        selection = (
            invocation.environment_selection
            or session.default_environment_selection
        )
        binding = await self.environment_provider.attach(selection)
        if invocation.environment_selection is not None:
            session.default_environment_selection = selection
        return replace(invocation, environment_binding=binding)


    async def _execute_turn(
        self,
        session: Session,
        turn_record: SessionTurnRecord,
        user_message: str,
        max_steps: int,
        control: TurnControl,
        invocation: TurnInvocation,
    ) -> SessionTurnOutcome:
        turn_runtime = self._session_turn_runtimes.get(
            session.id,
            self.turn_runtime,
        )
        turn_arguments = dict(
            conversation=session.conversation,
            user_message=user_message,
            max_steps=max_steps,
            control=control,
            context_state=session.context_state,
        )
        effective_invocation = invocation
        provider = effective_invocation.toolset_provider
        if provider is not None and session.capability_snapshot is not None:
            effective_invocation = replace(
                effective_invocation,
                toolset_provider=SnapshotToolsetProvider(
                    provider,
                    session.capability_snapshot,
                ),
            )
        turn_arguments["invocation"] = effective_invocation
        result = await turn_runtime.run(**turn_arguments)
        if isinstance(result.approval_request, ApprovalRequest):
            if result.status is not TurnStatus.BLOCKED:
                raise ValueError("ApprovalRequest 必须阻塞当前 Turn")
            session.pending_approval_id = result.approval_request.id
        session.context_state = result.context_state
        target_status, event_kind = TURN_STATUS_MAPPING[result.status]
        ended_at = datetime.now(timezone.utc)

        event = SessionEvent(
            kind=event_kind,
            session_id=session.id,
            reason=result.final_reason or "Turn completed",
            turn_id=turn_record.turn_id,
        )
        session.transition_to(target_status, event)
        turn_record.status = result.status.value
        turn_record.ended_at = ended_at
        turn_record.final_reason = result.final_reason

        return SessionTurnOutcome(record=turn_record, result=result)
