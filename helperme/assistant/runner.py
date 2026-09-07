from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.failures import assistant_failure_message
from helperme.assistant.management import ManagementSurface
from helperme.assistant.toolsets import ToolSurface
from helperme.runtime import AgentRuntime, RuntimeStatus
from helperme.runtime.model import CanonicalState


class SessionNotFoundError(LookupError):
    pass


async def resume_session(
    runtime: AgentRuntime,
    surface: ToolSurface,
    session_id: str,
    management: ManagementSurface,
) -> CanonicalState:
    """Select an existing Session and rebuild Host projections."""

    if not await runtime.session_exists(session_id):
        raise SessionNotFoundError(session_id)
    events = await runtime.snapshot(session_id)
    await surface.rehydrate(session_id, events)
    await management.rehydrate(session_id, events)
    return await runtime.state(session_id)


class SessionScheduler:
    """Advance one bound Session from committed facts."""

    def __init__(
        self,
        runtime: AgentRuntime,
        session_id: str,
        *,
        control: AssistantControlPlane,
        notify: Callable[[str, str], Awaitable[None] | None] | None = None,
        on_quiesced: (
            Callable[[str, CanonicalState], Awaitable[None] | None] | None
        ) = None,
        on_failed: Callable[[str, str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._runtime = runtime
        self._session_id = session_id
        self._control = control
        self._notify = notify
        self._on_quiesced = on_quiesced
        self._on_failed = on_failed
        self.before_advance = None
        self.retired = False
        self.propagate_failures = False
        self._task: asyncio.Task[bool] | None = None
        self._pending_wake = False
        self._failure: BaseException | None = None
        self._failure_event = asyncio.Event()
        self.changed = asyncio.Event()
        runtime.dispatcher.connect(self.wake, self._record_failure)

    async def wake(self, session_id: str) -> None:
        assert session_id == self._session_id
        self.changed.set()
        if self.retired:
            return
        if self._failure is not None:
            raise self._failure
        task = self._task
        # The completion callback owns the slot until it has observed the result.
        if task is not None:
            self._pending_wake = True
            return
        self._start()

    def _start(self) -> None:
        task = asyncio.create_task(
            self._advance_once(),
            name=f"agent-session:{self._session_id}",
        )
        self._task = task
        task.add_done_callback(self._task_done)

    async def _advance_once(self) -> bool:
        session_id = self._session_id
        if self.before_advance is not None and not await self.before_advance():
            return False
        try:
            advance = await self._runtime.advance(session_id)
        except Exception as error:
            if self.propagate_failures:
                raise
            message = assistant_failure_message(error)
            if message is None:
                raise
            # 已识别的模型失败只停这条 Session：它仍是 RUNNABLE，
            # 下一条外部事实会从同一个 trigger 重试。
            await self._emit(session_id, f"运行失败：{message}")
            await self._failed(session_id, message)
            return False
        if advance.step is not None:
            result = await self._control.after_committed_step(
                session_id,
                advance.step,
            )
            if result is not None:
                await self._emit(session_id, result.message)
        if advance.status is not RuntimeStatus.RUNNABLE:
            await self._quiesced(session_id)
        return advance.status is RuntimeStatus.RUNNABLE

    async def _emit(self, session_id: str, message: str) -> None:
        if self._notify is None:
            return
        notified = self._notify(session_id, message)
        if isinstance(notified, Awaitable):
            await notified

    async def _quiesced(self, session_id: str) -> None:
        """本次推进没有留下待办。订阅者自己判断这是否算一件事做完了。"""

        if self._on_quiesced is None:
            return
        observed = self._on_quiesced(
            session_id,
            await self._runtime.state(session_id),
        )
        if isinstance(observed, Awaitable):
            await observed

    async def _failed(self, session_id: str, message: str) -> None:
        """推进因已识别的失败停下。与静止是两件事，订阅者分开处理。"""

        if self._on_failed is None:
            return
        observed = self._on_failed(session_id, message)
        if isinstance(observed, Awaitable):
            await observed

    def _task_done(self, task: asyncio.Task[bool]) -> None:
        self.changed.set()
        self._task = None
        pending_wake = self._pending_wake
        self._pending_wake = False
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._record_failure(error)
        elif not self.retired and self._failure is None and (task.result() or pending_wake):
            self._start()

    def _record_failure(self, error: BaseException) -> None:
        self.changed.set()
        if self._failure is None:
            self._failure = error
            self._failure_event.set()

    async def wait_failure(self) -> BaseException:
        await self._failure_event.wait()
        assert self._failure is not None
        return self._failure

    @property
    def idle(self) -> bool:
        return self._task is None and self._runtime.dispatcher.active_count == 0

    async def close(self) -> None:
        await self._runtime.dispatcher.close()
        task = self._task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def pending_authorization_ids(state: CanonicalState) -> tuple[str, ...]:
    return tuple(
        item.split(":", 1)[1]
        for item in state.waiting_for
        if item.startswith("authorization:")
    )
