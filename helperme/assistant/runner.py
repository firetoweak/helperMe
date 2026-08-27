from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.management import ManagementSurface
from helperme.assistant.toolsets import ToolSurface
from helperme.llm.api import (
    InvalidLLMResponse,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from helperme.runtime import AgentRuntime, RuntimeStatus
from helperme.runtime.model import CanonicalState


class SessionNotFoundError(LookupError):
    pass


MODEL_DECISION_ERRORS = (
    InvalidLLMResponse,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)


async def resume_session(
    runtime: AgentRuntime,
    surface: ToolSurface,
    session_id: str,
    management: ManagementSurface | None = None,
) -> CanonicalState:
    """Select an existing Session and rebuild Host projections."""

    if not await runtime.session_exists(session_id):
        raise SessionNotFoundError(session_id)
    events = await runtime.snapshot(session_id)
    await surface.rehydrate(session_id, events)
    if management is not None:
        await management.rehydrate(session_id, events)
    return await runtime.state(session_id)


class SessionScheduler:
    """Activate independent Sessions concurrently from committed facts."""

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        control: AssistantControlPlane | None = None,
        notify: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._runtime = runtime
        self._control = control
        self._notify = notify
        self._tasks: dict[str, asyncio.Task[bool]] = {}
        self._pending_wakes: set[str] = set()
        self._changed = asyncio.Event()
        self._failure: BaseException | None = None
        self._failure_event = asyncio.Event()
        runtime.dispatcher.connect(self.wake, self._record_failure)

    async def wake(self, session_id: str) -> None:
        if self._failure is not None:
            raise self._failure
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            self._pending_wakes.add(session_id)
            return
        self._start(session_id)

    def _start(self, session_id: str) -> None:
        task = asyncio.create_task(
            self._advance_once(session_id),
            name=f"agent-session:{session_id}",
        )
        self._tasks[session_id] = task
        task.add_done_callback(
            lambda done, current=session_id: self._task_done(current, done)
        )
        self._changed.set()

    async def _advance_once(self, session_id: str) -> bool:
        step = await self._runtime.advance(session_id)
        if step is not None and self._control is not None:
            result = await self._control.after_committed_step(
                session_id,
                step,
            )
            if result is not None and self._notify is not None:
                notified = self._notify(result.message)
                if isinstance(notified, Awaitable):
                    await notified
        return (await self._runtime.state(session_id)).status is RuntimeStatus.RUNNABLE

    def _task_done(self, session_id: str, task: asyncio.Task[bool]) -> None:
        if self._tasks.get(session_id) is not task:
            return
        self._tasks.pop(session_id)
        pending_wake = session_id in self._pending_wakes
        self._pending_wakes.discard(session_id)
        if task.cancelled():
            self._changed.set()
            return
        error = task.exception()
        if error is not None:
            self._record_failure(error)
        elif self._failure is None and (task.result() or pending_wake):
            self._start(session_id)
        self._changed.set()

    def _record_failure(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = error
            self._failure_event.set()

    async def join(self) -> None:
        """Wait for active Sessions and Commands; primarily an observation aid."""

        while True:
            self._changed.clear()
            if self._failure is not None:
                raise self._failure
            if not self._tasks and self._runtime.dispatcher.active_count == 0:
                await asyncio.sleep(0)
                if not self._tasks and self._runtime.dispatcher.active_count == 0:
                    break
                continue
            changed = asyncio.create_task(self._changed.wait())
            failed = asyncio.create_task(self._failure_event.wait())
            done, pending = await asyncio.wait(
                (changed, failed),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if failed in done and self._failure is not None:
                raise self._failure
        if self._failure is not None:
            raise self._failure

    async def close(self) -> None:
        await self._runtime.dispatcher.close()
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def pending_authorization_ids(state: CanonicalState) -> tuple[str, ...]:
    return tuple(
        item.split(":", 1)[1]
        for item in state.waiting_for
        if item.startswith("authorization:")
    )
