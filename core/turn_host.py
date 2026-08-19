from __future__ import annotations

from typing import Protocol

from core.session import SessionTurnOutcome
from core.tools_runtime.turn_invocation import TurnInvocation


class TurnHost(Protocol):
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

    def validate_turn(
        self,
        session_id: str,
        turn_id: str,
        user_message: str,
    ) -> None:
        ...

    async def execute(
        self,
        session_id: str,
        turn_id: str,
        user_message: str,
        max_steps: int | None,
        invocation: TurnInvocation,
    ) -> SessionTurnOutcome:
        ...
