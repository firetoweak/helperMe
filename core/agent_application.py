from __future__ import annotations

from core.session import SessionRunOutcome, SessionRuntime
from core.session.state import SessionStatus
from core.tools_runtime.run_invocation import RunInvocation

DEFAULT_MAX_ROUNDS = 50


class AgentApplication:
    def __init__(
        self,
        session_runtime: SessionRuntime,
        system_prompt: str,
        default_max_rounds: int = DEFAULT_MAX_ROUNDS,
    ):
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        if default_max_rounds < 1:
            raise ValueError("default_max_rounds 必须大于 0")

        self._session_runtime = session_runtime
        self._system_prompt = system_prompt
        self._default_max_rounds = default_max_rounds

    def create_session(self, session_id: str) -> str:
        self._session_runtime.create_session(
            session_id=session_id,
            system_prompt=self._system_prompt,
        )
        return session_id

    def start(self, session_id, run_id, message, max_rounds=None):
        return self._session_runtime.start(
            session_id,
            run_id,
            message,
            self._resolve_max_rounds(max_rounds),
        )

    def resume(self, session_id, run_id, message, max_rounds=None):
        return self._session_runtime.resume(
            session_id,
            run_id,
            message,
            self._resolve_max_rounds(max_rounds),
        )

    def require_session(self, session_id: str) -> None:
        self._session_runtime.get_session(session_id)

    def validate_run(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
    ) -> None:
        self._session_runtime.validate_run_input(run_id, user_message)
        session = self._session_runtime.get_session(session_id)
        if session.status is SessionStatus.RUNNING:
            raise ValueError(f"Session 已在执行: {session_id}")
        if any(record.run_id == run_id for record in session.run_records):
            raise ValueError(f"重复 run_id: {run_id}")

    def execute(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int | None,
        invocation: RunInvocation,
    ) -> SessionRunOutcome:
        session = self._session_runtime.get_session(session_id)
        use_case = (
            self._session_runtime.resume
            if session.status is SessionStatus.INTERRUPTED
            else self._session_runtime.start
        )
        return use_case(
            session_id,
            run_id,
            user_message,
            self._resolve_max_rounds(max_rounds),
            invocation=invocation,
        )

    def _resolve_max_rounds(self, max_rounds: int | None) -> int:
        resolved = (
            self._default_max_rounds
            if max_rounds is None
            else max_rounds
        )
        if resolved < 1:
            raise ValueError("max_rounds 必须大于 0")
        return resolved

    def request_interrupt(self, session_id, reason=None):
        self._session_runtime.request_interrupt(session_id, reason)

    def delete_session(self, session_id: str) -> None:
        self._session_runtime.delete_session(session_id)
