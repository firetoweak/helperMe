from __future__ import annotations

from dataclasses import dataclass

from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.runner import SessionScheduler
from helperme.runtime import AgentRuntime
from helperme.runtime.model import CanonicalState


@dataclass(frozen=True, slots=True)
class SettledSession:
    state: CanonicalState
    control_message: str | None = None


async def settle_session(
    runtime: AgentRuntime,
    session_id: str,
    *,
    control: AssistantControlPlane | None = None,
) -> SettledSession:
    messages: list[str] = []
    scheduler = SessionScheduler(
        runtime,
        control=control,
        notify=messages.append,
    )
    try:
        await scheduler.wake(session_id)
        await scheduler.join()
        return SettledSession(
            await runtime.state(session_id),
            messages[-1] if messages else None,
        )
    finally:
        await scheduler.close()
