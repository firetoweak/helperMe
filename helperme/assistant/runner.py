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
    """Wake Sessions from committed facts; execute at most one Step per wake."""

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
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._processing = 0
        self._worker: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._failure_event = asyncio.Event()
        runtime.dispatcher.connect(self.wake, self._record_failure)

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._run(),
                name="agent-session-scheduler",
            )
            self._worker.add_done_callback(self._worker_done)

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._record_failure(error)

    async def wake(self, session_id: str) -> None:
        if self._failure is not None:
            raise self._failure
        self.start()
        if session_id in self._queued:
            return
        self._queued.add(session_id)
        await self._queue.put(session_id)

    async def _run(self) -> None:
        while True:
            session_id = await self._queue.get()
            self._queued.discard(session_id)
            self._processing += 1
            try:
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
                state = await self._runtime.state(session_id)
                if state.status is RuntimeStatus.RUNNABLE:
                    await self.wake(session_id)
            finally:
                self._processing -= 1
                self._queue.task_done()

    def _record_failure(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = error
            self._failure_event.set()

    async def join(self) -> None:
        """Wait for queued Step activations; primarily a test/CLI observation aid."""

        while True:
            queue_idle = asyncio.create_task(self._queue.join())
            failed = asyncio.create_task(self._failure_event.wait())
            done, pending = await asyncio.wait(
                (queue_idle, failed),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if failed in done and self._failure is not None:
                raise self._failure
            await asyncio.sleep(0)
            if (
                self._processing == 0
                and self._queue.empty()
                and self._runtime.dispatcher.active_count == 0
            ):
                break
        if self._failure is not None:
            raise self._failure

    async def close(self) -> None:
        await self._runtime.dispatcher.close()
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass


def pending_authorization_ids(state: CanonicalState) -> tuple[str, ...]:
    return tuple(
        item.split(":", 1)[1]
        for item in state.waiting_for
        if item.startswith("authorization:")
    )
