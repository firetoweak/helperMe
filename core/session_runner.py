from __future__ import annotations



from core.tools_runtime.tools_runner import RunControl, ToolsRunner
from core.session_state import (
    Session,
    SessionEvent,
    SessionEventSource,
    SessionEventType,
)


class SessionRuntime:
    def __init__(self, tools_runner: ToolsRunner):
        self.tools_runner = tools_runner
        self.sessions: dict[str, Session] = {}
        self.active_controls: dict[str, RunControl] = {}

    def create_session(self, session_id: str) -> Session:
        if not session_id or not session_id.strip():
            raise ValueError("session_id 不能为空")
        if session_id in self.sessions:
            raise ValueError(f"重复 session_id: {session_id}")

        session = Session(id=session_id)
        event = SessionEvent(
            kind=SessionEventType.CREATED,
            session_id=session.id,
            source=SessionEventSource.RUNTIME,
            reason="Session created",
        )

        session.record_event(event)
        self.sessions[session.id] = session
        return session
