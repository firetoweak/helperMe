"""Private, inherited process pipe. Application messages only."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import traceback
from uuid import uuid4


@dataclass(frozen=True)
class ProcessFailure:
    exception_type: str
    message: str
    traceback: str

    @classmethod
    def capture(cls, error: BaseException) -> ProcessFailure:
        return cls(
            f"{type(error).__module__}.{type(error).__qualname__}",
            str(error),
            "".join(traceback.format_exception(error)),
        )

    def render(self) -> str:
        return f"{self.exception_type}: {self.message}\n{self.traceback}"


class WorkerFailed(RuntimeError):
    def __init__(self, session_id: str, failure: ProcessFailure) -> None:
        self.session_id = session_id
        self.failure = failure
        super().__init__(
            f"{session_id}: {failure.exception_type}: {failure.message}\n{failure.traceback}"
        )


class PipePeer:
    def __init__(self, connection, handler, signal, *, peer_alive=None) -> None:
        self.connection = connection
        self.peer_alive = peer_alive
        self.handler = handler
        self.signal = signal
        self.pending: dict[str, asyncio.Future] = {}
        self.tasks: set[asyncio.Task] = set()
        self.stopped = False
        self.failure: BaseException | None = None
        self.send_lock = asyncio.Lock()

    async def send(self, message) -> None:
        async with self.send_lock:
            await asyncio.to_thread(self.connection.send, message)

    async def request(self, operation: str, session_id: str, arguments: dict):
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send(("request", request_id, operation, session_id, arguments))
            return await future
        finally:
            del self.pending[request_id]

    async def _handle(self, request_id, operation, session_id, arguments):
        result = await self.handler(operation, session_id, arguments)
        await self.send(("response", request_id, result))

    def _done(self, task):
        self.tasks.remove(task)
        if not task.cancelled() and task.exception() is not None:
            self.failure = task.exception()

    def _receive(self):
        if self.connection.poll(0.1):
            return self.connection.recv()
        # A process killed during spawn may leave an unclaimed duplicated pipe
        # handle in the parent. Drain buffered messages, then use process death
        # as EOF rather than waiting forever for that duplicate to close.
        if self.peer_alive is not None and not self.peer_alive():
            raise EOFError("peer process exited")
        return None

    async def run(self):
        while not self.stopped:
            if self.failure is not None:
                raise self.failure
            message = await asyncio.to_thread(self._receive)
            if message is None:
                continue
            kind, *payload = message
            if kind == "request":
                task = asyncio.create_task(self._handle(*payload))
                self.tasks.add(task)
                task.add_done_callback(self._done)
            elif kind == "response":
                request_id, result = payload
                self.pending[request_id].set_result(result)
            else:
                await self.signal(kind, *payload)

    async def close(self, error: BaseException):
        self.stopped = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
