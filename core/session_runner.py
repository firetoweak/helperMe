from __future__ import annotations


from dataclasses import dataclass

from core.tools_runtime.run_runtime import (
    RunControl,
    RunResult,
    RunRuntime,
    RunStatus,
)
from core.session_state import (
    Session,
    SessionEvent,
    SessionEventType,
    SessionRunRecord,
    SessionStatus,
)
from datetime import datetime, timezone


RUN_STATUS_MAPPING = {
    RunStatus.COMPLETED: (
        SessionStatus.COMPLETED,
        SessionEventType.COMPLETED,
    ),
    RunStatus.INTERRUPTED: (
        SessionStatus.INTERRUPTED,
        SessionEventType.INTERRUPTED,
    ),
    RunStatus.BLOCKED: (
        SessionStatus.BLOCKED,
        SessionEventType.BLOCKED,
    ),
    RunStatus.FAILED: (
        SessionStatus.FAILED,
        SessionEventType.FAILED,
    ),
}


@dataclass(frozen=True)
class SessionRunOutcome:
    record: SessionRunRecord
    result: RunResult



class SessionRuntime:
    def __init__(self, run_runtime: RunRuntime):
        self.run_runtime = run_runtime
        self.sessions: dict[str, Session] = {}
        self.active_controls: dict[str, RunControl] = {}

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

        session = Session(id=session_id)
        session.conversation.set_system_prompt(system_prompt)
        event = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id=session.id,
            reason="Session created",
        )

        session.record_event(event)
        self.sessions[session.id] = session
        return session

    def start(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int = 20,
    ) -> SessionRunOutcome:
        if not session_id or not session_id.strip():
            raise ValueError("session_id 不能为空")
        if not run_id or not run_id.strip():
            raise ValueError("run_id 不能为空")
        if not user_message or not user_message.strip():
            raise ValueError("user_message 不能为空")

        if session_id not in self.sessions:
            raise KeyError(f"Session 不存在: {session_id}")

        session = self.sessions[session_id]
        if session.status not in {
            SessionStatus.PENDING,
            SessionStatus.COMPLETED,
        }:
            raise ValueError(
                "Session 状态必须为 pending 或 completed，"
                f"当前为: {session.status.value}"
            )

        return self._begin_and_execute_run(
            session=session,
            run_id=run_id,
            user_message=user_message,
            max_rounds=max_rounds,
            event_kind=SessionEventType.STARTED,
            event_reason="Session started",
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


    def resume(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int = 20,
    ) -> SessionRunOutcome:
        if not session_id or not session_id.strip():
            raise ValueError("session_id 不能为空")
        if not run_id or not run_id.strip():
            raise ValueError("run_id 不能为空")
        if not user_message or not user_message.strip():
            raise ValueError("user_message 不能为空")
        if session_id not in self.sessions:
            raise KeyError(f"Session 不存在: {session_id}")

        session = self.sessions[session_id]
        if session.status != SessionStatus.INTERRUPTED:
            raise ValueError(
                f"Session 状态必须为 interrupted，当前为: {session.status.value}"
            )

        return self._begin_and_execute_run(
            session=session,
            run_id=run_id,
            user_message=user_message,
            max_rounds=max_rounds,
            event_kind=SessionEventType.RESUMED,
            event_reason="Session resumed",
        )

    def _begin_and_execute_run(
        self,
        session: Session,
        run_id: str,
        user_message: str,
        max_rounds: int,
        event_kind: SessionEventType,
        event_reason: str,
    ) -> SessionRunOutcome:
        if session.id in self.active_controls:
            raise ValueError(f"Session 已有正在执行的 run: {session.id}")
        if any(record.run_id == run_id for record in session.run_records):
            raise ValueError(f"重复 run_id: {run_id}")

        run_control = RunControl()
        run_record = SessionRunRecord(
            run_id=run_id,
            status="running",
            started_at=datetime.now(timezone.utc),
            ended_at=None,
            final_reason=None,
        )
        event = SessionEvent(
            kind=event_kind,
            session_id=session.id,
            reason=event_reason,
            run_id=run_id,
        )
        session.transition_to(SessionStatus.RUNNING, event)
        session.run_records.append(run_record)
        self.active_controls[session.id] = run_control

        try:
            return self._execute_run(
                session=session,
                run_record=run_record,
                user_message=user_message,
                max_rounds=max_rounds,
                control=run_control,
            )
        finally:
            del self.active_controls[session.id]


    def _execute_run(
        self,
        session: Session,
        run_record: SessionRunRecord,
        user_message: str,
        max_rounds: int,
        control: RunControl,
    ) -> SessionRunOutcome:
        result = self.run_runtime.run(
            conversation=session.conversation,
            user_message=user_message,
            max_rounds=max_rounds,
            control=control,
            context_state=session.context_state,
        )
        session.context_state = result.context_state
        target_status, event_kind = RUN_STATUS_MAPPING[result.status]
        ended_at = datetime.now(timezone.utc)

        event = SessionEvent(
            kind=event_kind,
            session_id=session.id,
            reason=result.final_reason or "Run completed",
            run_id=run_record.run_id,
        )
        session.transition_to(target_status, event)
        run_record.status = result.status.value
        run_record.ended_at = ended_at
        run_record.final_reason = result.final_reason

        return SessionRunOutcome(record=run_record, result=result)
