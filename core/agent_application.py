from __future__ import annotations

from typing import TYPE_CHECKING

from core.session import SessionRuntime

if TYPE_CHECKING:
    from core.goals import GoalApplicationService



class AgentApplication:
    def __init__(
        self,
        session_runtime: SessionRuntime,
        system_prompt: str,
        goal_application: GoalApplicationService | None = None,
    ):
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")

        self._session_runtime = session_runtime
        self._system_prompt = system_prompt
        self._goal_application = goal_application

    @property
    def goals(self) -> GoalApplicationService:
        if self._goal_application is None:
            raise RuntimeError("Goal capability is not configured")
        return self._goal_application

    def create_session(self, session_id: str) -> str:
        self._session_runtime.create_session(
            session_id=session_id,
            system_prompt=self._system_prompt,
            )
        return session_id

    def start(self, session_id, run_id, message, max_rounds=50):
        return self._session_runtime.start(
            session_id, run_id, message, max_rounds
        )

    def resume(self, session_id, run_id, message, max_rounds=50):
        return self._session_runtime.resume(
            session_id, run_id, message, max_rounds
        )

    def request_interrupt(self, session_id, reason=None):
        self._session_runtime.request_interrupt(session_id, reason)

    def delete_session(self, session_id: str) -> None:
        self._session_runtime.delete_session(session_id)
