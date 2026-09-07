from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import multiprocessing
import os

from helperme.assistant.delivery import emit_delivery
from helperme.assistant.ipc import PipePeer, ProcessFailure, WorkerFailed
from helperme.assistant.session_store import SessionStore
from helperme.assistant.subagent import persist_return, report_arguments, return_data
from helperme.assistant.worker import worker_main
from helperme.runtime import SqliteJournal
from helperme.sandbox.local.windows_job import WindowsJob


@dataclass
class Worker:
    process: object
    peer: PipePeer
    reader: asyncio.Task | None = None
    requests: int = 0
    idle_revision: int | None = None
    stopping: bool = False
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    failure: WorkerFailed | None = None
    transition: asyncio.Event = field(default_factory=asyncio.Event)
    returned: tuple[str, dict] | None = None
    reclaimed: bool = False


class HostSupervisor:
    """One local owner, one process per active Session. No Runtime projection."""

    def __init__(
        self,
        store: SessionStore,
        config_factory,
        home,
        sink,
        *,
        context_usage_sink=None,
        subagent_activity_sink=None,
    ):
        self.store = store
        self.config_factory = config_factory
        self.home = home
        self.sink = sink
        self.context_usage_sink = context_usage_sink
        self.subagent_activity_sink = subagent_activity_sink
        self.workers: dict[str, Worker] = {}
        self.watchers: set[asyncio.Task] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.failures: asyncio.Queue[WorkerFailed] = asyncio.Queue()
        self.reclaimed: set[str] = set()
        self.closed = False
        self.job = WindowsJob.create() if os.name == "nt" else None

    async def _route(self, operation, session_id, arguments):
        if operation == "output":
            await emit_delivery(self.sink, session_id, arguments["text"])
            return None
        if operation == "create_child":
            # Identity is stable; an existing child is resumed, never replaced.
            async with self.locks.setdefault(session_id, asyncio.Lock()):
                if not self.store.path(session_id).parent.exists():
                    await self.store.create(session_id, initial_fact=arguments)
            # delegate acknowledges durable creation, not successful initialization.
            activation = asyncio.create_task(
                self.request("fact", session_id, arguments)
            )
            self._track(session_id, activation)
            return None
        if operation == "reclaim_child":
            await self._reclaim_child(session_id, arguments)
            return None
        return await self.request(operation, session_id, arguments)

    async def _start(self, session_id):
        path = self.store.require(session_id)
        context = multiprocessing.get_context("spawn")
        local, remote = context.Pipe()
        worker = None

        async def signal(kind, *values):
            if kind == "usage":
                if self.context_usage_sink is not None:
                    self.context_usage_sink(*values)
            elif kind == "activity":
                if self.subagent_activity_sink is not None:
                    self.subagent_activity_sink(*values)
            elif kind == "idle":
                worker.idle_revision = values[0]
                await self._stop_idle(worker)
            elif kind == "busy":
                worker.idle_revision = None
                worker.stopping = False
                worker.transition.set()
            elif kind == "stopping":
                worker.stopping = True
            elif kind == "failure":
                worker.failure = WorkerFailed(session_id, values[0])
            elif kind == "return":
                worker.returned = tuple(values)
            else:
                raise ValueError(f"Unknown Host signal: {kind}")

        peer = PipePeer(local, self._route, signal)
        admitted = context.Event()
        process = context.Process(
            target=worker_main,
            args=(
                remote,
                session_id,
                path,
                self.config_factory,
                self.home.root,
                admitted,
            ),
            name=f"session:{session_id}",
        )
        process.start()
        try:
            if self.job is not None:
                self.job.assign(process.pid)
            admitted.set()
        except BaseException:
            process.terminate()
            await asyncio.to_thread(process.join)
            process.close()
            local.close()
            remote.close()
            raise
        remote.close()
        worker = Worker(process, peer)
        self.workers[session_id] = worker
        worker.reader = asyncio.create_task(self._watch(session_id, worker))
        self._track(session_id, worker.reader)
        return worker

    def _track(self, session_id, task):
        self.watchers.add(task)

        def watched(task):
            self.watchers.remove(task)
            if (
                not task.cancelled()
                and task.exception() is not None
                and not isinstance(task.exception(), WorkerFailed)
                and session_id not in self.reclaimed
            ):
                # WorkerFailed is already exposed by that Worker's lifecycle watcher.
                self.failures.put_nowait(
                    WorkerFailed(session_id, ProcessFailure.capture(task.exception()))
                )

        task.add_done_callback(watched)

    async def _watch(self, session_id, worker):
        try:
            try:
                await worker.peer.run()
            except (EOFError, ConnectionResetError, BrokenPipeError):
                # Pipe EOF is a process lifecycle fact, not a Command outcome.
                pass
            except Exception as error:
                # Host-side IPC or routing failure belongs to this Worker boundary.
                worker.failure = WorkerFailed(session_id, ProcessFailure.capture(error))
                worker.process.terminate()
            await asyncio.to_thread(worker.process.join)
            if (
                worker.failure is None
                and worker.process.exitcode != 0
                and not self.closed
                and not worker.reclaimed
                and session_id not in self.reclaimed
            ):
                worker.failure = WorkerFailed(
                    session_id,
                    ProcessFailure(
                        "ProcessExit",
                        f"exit code {worker.process.exitcode}",
                        "",
                    ),
                )
        finally:
            worker.stopping = True
            await worker.peer.close(worker.failure or RuntimeError("Worker exited"))
            worker.peer.connection.close()
            worker.process.close()
            self.workers.pop(session_id)
            worker.exited.set()
            worker.transition.set()
        # Release the dead Worker and its pending requests before waking the parent.
        if worker.failure is not None:
            self.failures.put_nowait(worker.failure)
        if worker.returned is not None and not self.closed:
            parent, arguments = worker.returned
            await self.request("fact", parent, arguments)

    async def _reclaim_child(self, session_id, arguments):
        """Stop the child Worker, persist cancel if needed, then report to parent.

        Host writes the child's Journal only after that Session has no Worker.
        """

        parent_session_id = arguments["parent_session_id"]
        reason = arguments.get("reason")
        self.reclaimed.add(session_id)
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            worker = self.workers.get(session_id)
            if worker is not None and not worker.exited.is_set():
                worker.reclaimed = True
                if worker.process.is_alive():
                    worker.process.terminate()
                await worker.exited.wait()
            returned = await persist_return(
                SqliteJournal(self.store.require(session_id)),
                session_id,
                return_data(
                    session_id,
                    reported=False,
                    summary=None,
                    failure=None,
                    cancelled=True,
                    reason=reason,
                ),
            )
        await self.request(
            "fact",
            parent_session_id,
            report_arguments(session_id, returned.payload.data),
        )

    async def _stop_idle(self, worker):
        if (
            worker.requests == 0
            and worker.idle_revision is not None
            and not worker.stopping
        ):
            worker.stopping = True
            worker.transition.clear()
            await worker.peer.send(("stop", worker.idle_revision))

    async def request(self, operation, session_id, arguments):
        assert not self.closed
        lock = self.locks.setdefault(session_id, asyncio.Lock())
        while True:
            async with lock:
                worker = self.workers.get(session_id)
                if worker is None:
                    worker = await self._start(session_id)
                if not worker.stopping:
                    worker.requests += 1
                    worker.idle_revision = None
                    break
            await worker.transition.wait()
        try:
            return await worker.peer.request(operation, session_id, arguments)
        finally:
            worker.requests -= 1
            if not worker.exited.is_set():
                await self._stop_idle(worker)

    async def create(self, session_id):
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            await self.store.create(session_id)
        return await self.request("create", session_id, {})

    async def resume(self, session_id):
        return await self.request("resume", session_id, {})

    async def view(self, session_id):
        return await self.request("view", session_id, {})

    async def receive_user_message(self, session_id, content, **kwargs):
        await self.request(
            "receive_user_message", session_id, dict(content=content, **kwargs)
        )

    async def resolve_authorizations(self, session_id, *, approved):
        await self.request(
            "resolve_authorizations", session_id, dict(approved=approved)
        )

    async def resolve_control(self, session_id, *, approved):
        return await self.request(
            "resolve_control", session_id, dict(approved=approved)
        )

    async def wait_failure(self):
        return await self.failures.get()

    async def close(self):
        self.closed = True
        workers = tuple(self.workers.values())
        for worker in workers:
            if worker.process.is_alive():
                worker.process.terminate()
        await asyncio.gather(*self.watchers, return_exceptions=True)
        if self.job is not None:
            self.job.close()
            self.job = None
