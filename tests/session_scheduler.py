from __future__ import annotations

import asyncio
from dataclasses import dataclass

from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.runner import SessionScheduler
from helperme.runtime import AgentRuntime
from helperme.runtime.model import CanonicalState


class SettlingScheduler(SessionScheduler):
    async def join(self) -> None:
        while True:
            await asyncio.sleep(0)
            if self._failure is not None:
                raise self._failure
            tasks = (
                *self._tasks.values(),
                *self._runtime.dispatcher._tasks.values(),
            )
            if not tasks:
                await asyncio.sleep(0)
                if not self._tasks and not self._runtime.dispatcher._tasks:
                    return
                continue
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)


class RecordingScheduler(SettlingScheduler):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.woken: list[str] = []

    async def wake(self, session_id: str) -> None:
        self.woken.append(session_id)
        await super().wake(session_id)


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
    scheduler = SettlingScheduler(
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
