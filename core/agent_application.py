from __future__ import annotations

from core.session_runner import SessionRuntime



class AgentApplication:
    def __init__(
        self,
        session_runtime: SessionRuntime,
        system_prompt: str,
    ):
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")

        self._session_runtime = session_runtime
        self._system_prompt = system_prompt

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