from __future__ import annotations

from typing import Protocol

from core.session import SessionRunOutcome
from core.tools_runtime.run_invocation import RunInvocation


class RunHost(Protocol):
    def create_session(self, session_id: str) -> str:
        ...

    def delete_session(self, session_id: str) -> None:
        ...

    def require_session(self, session_id: str) -> None:
        ...

    def request_interrupt(
        self,
        session_id: str,
        reason: str | None = None,
    ) -> None:
        ...

    def validate_run(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
    ) -> None:
        ...

    def execute(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int | None,
        invocation: RunInvocation,
    ) -> SessionRunOutcome:
        ...
