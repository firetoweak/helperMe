from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
from inspect import isawaitable
import multiprocessing
import os

from helperme.assistant.auto_authorize import (
    AutoAuthorizeStore,
    auto_grant_for_owners,
)
from helperme.assistant.session_pause import SessionPauseStore
from helperme.assistant.compact.host import CompactHost
from helperme.assistant.delivery import emit_delivery
from helperme.assistant.host.ipc import PipePeer, ProcessFailure, WorkerFailed
from helperme.assistant.host.llm_port import complete_llm_chat
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.sessions import session_view
from helperme.assistant.subagent.subagent import (
    persist_return,
    project_pending,
    report_arguments,
    return_data,
)
from helperme.assistant.workspaces import UnboundSessionError, bound_workspace_id
from helperme.assistant.host.spawn import start_worker
from helperme.assistant.host.worker import worker_main
from helperme.runtime import SqliteJournal, replay
from helperme.sandbox.local.windows_job import WindowsJob
from helperme.sandbox.registry import WorkspaceRegistry


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
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    idle_has_active_subagents: bool = False
    running: bool = False
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
        llm,
        context_usage_sink=None,
        subagent_activity_sink=None,
        conversation_status_sink=None,
        tool_progress_sink=None,
        authorization_required_sink=None,
        preview_sink=None,
        thinking_sink=None,
        session_activity_sink=None,
        session_failed_sink=None,
        workspaces=None,
    ):
        self.store = store
        self.config_factory = config_factory
        self.home = home
        self.sink = sink
        self.llm = llm
        self.context_usage_sink = context_usage_sink
        self.subagent_activity_sink = subagent_activity_sink
        self.conversation_status_sink = conversation_status_sink
        self.tool_progress_sink = tool_progress_sink
        self.authorization_required_sink = authorization_required_sink
        self.preview_sink = preview_sink
        self.thinking_sink = thinking_sink
        self.session_activity_sink = session_activity_sink
        self.session_failed_sink = session_failed_sink
        self.workers: dict[str, Worker] = {}
        self.watchers: set[asyncio.Task] = set()
        self.locks: dict[str, asyncio.Lock] = {}
        self.failures: asyncio.Queue[WorkerFailed] = asyncio.Queue()
        self.reclaimed: set[str] = set()
        self.selections: dict[str, str] = {}
        self.selecting: dict[str, str] = {}
        self.selection_locks: dict[str, asyncio.Lock] = {}
        self.closed = False
        self.compact = CompactHost(self)
        self.job = WindowsJob.create() if os.name == "nt" else None
        self._auto_authorize = AutoAuthorizeStore(store.root)
        self._pause = SessionPauseStore(store.root)
        # Worker 内存态的镜像：读会话不必为了它唤醒 Worker。
        self._control_approvals: dict[str, object] = {}
        self.workspaces = (
            workspaces
            if workspaces is not None
            else WorkspaceRegistry.load(home.workspaces_path)
        )

    async def _route(self, operation, session_id, arguments):
        if operation == "llm_chat":
            worker = self.workers[session_id]

            async def on_delta(text):
                await worker.peer.send(("delta", worker.peer.active_request_id, text))

            async def on_reasoning_delta(text):
                await worker.peer.send(
                    ("reasoning_delta", worker.peer.active_request_id, text)
                )

            return await complete_llm_chat(
                self.llm, arguments, on_delta, on_reasoning_delta
            )
        if operation == "is_paused":
            return self.is_paused(session_id)
        if operation == "compact_boundary":
            return await self.compact.boundary(session_id, arguments)
        if operation == "compact_complete":
            return await self.compact.complete(session_id, arguments)
        if operation == "output":
            if self.compact.store.reader_job(session_id) is not None:
                return None
            await emit_delivery(
                self.sink,
                session_id,
                arguments["output_id"],
                arguments["text"],
            )
            return None
        if operation == "create_child":
            # Identity is stable; an existing child is resumed, never replaced.
            initial_fact = dict(arguments)
            child_workspace_id = await self.bound_workspace_id(
                initial_fact["data"]["parent_session_id"]
            )
            async with self.locks.setdefault(session_id, asyncio.Lock()):
                if not self.store.path(session_id).parent.exists():
                    await self.store.create(
                        session_id,
                        workspace_id=child_workspace_id,
                        initial_fact=initial_fact,
                    )
            # delegate acknowledges durable creation, not successful initialization.
            activation = asyncio.create_task(
                self.request("fact", session_id, initial_fact)
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
                if (
                    self.context_usage_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    emitted = self.context_usage_sink(*values)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "activity":
                if (
                    self.subagent_activity_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    self.subagent_activity_sink(
                        *values
                    )
            elif kind == "tool":
                if (
                    self.tool_progress_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    emitted = self.tool_progress_sink(*values)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "authorization":
                if (
                    self.authorization_required_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    emitted = self.authorization_required_sink(*values)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "preview":
                if (
                    self.preview_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    emitted = self.preview_sink(*values)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "thinking":
                if (
                    self.thinking_sink is not None
                    and self.compact.store.reader_job(session_id) is None
                ):
                    emitted = self.thinking_sink(*values)
                    if isawaitable(emitted):
                        await emitted
            elif kind == "session_failed":
                await self._emit_session_failed(*values)
            elif kind == "control_approval":
                self._control_approvals[session_id] = values[0]
            elif kind == "idle":
                was_running = worker.running
                worker.idle_revision = values[0]
                worker.idle_has_active_subagents = values[1]
                worker.running = False
                worker.changed.set()
                await self._stop_idle(session_id, worker)
                if was_running:
                    await self._emit_session_activity(session_id, "idle")
            elif kind == "busy":
                worker.idle_revision = None
                worker.idle_has_active_subagents = False
                worker.running = True
                worker.stopping = False
                worker.transition.set()
                worker.changed.set()
                await self._emit_session_activity(session_id, "running")
            elif kind == "stopping":
                worker.stopping = True
                worker.changed.set()
            elif kind == "failure":
                worker.failure = WorkerFailed(session_id, values[0])
                worker.changed.set()
            elif kind == "return":
                worker.returned = tuple(values)
            else:
                raise ValueError(f"Unknown Host signal: {kind}")

        peer = PipePeer(
            local, self._route, signal, peer_alive=lambda: worker.process.is_alive()
        )
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
        start_worker(process, extra_handles=(local, remote))
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
            self._control_approvals.pop(session_id, None)
            worker.exited.set()
            worker.transition.set()
            worker.changed.set()
        # Release the dead Worker and its pending requests before waking the parent.
        if worker.failure is not None:
            self.compact.store.fail(session_id, asdict(worker.failure.failure))
            compact_job = self.compact.store.reader_job(session_id)
            if compact_job is not None:
                self.compact.notify_status(compact_job["source"])
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

    def _selected(self, session_id):
        return session_id in self.selections.values()

    def _being_selected(self, session_id):
        return session_id in self.selecting.values()

    async def _stop_idle(self, session_id, worker):
        if (
            worker.requests == 0
            and worker.idle_revision is not None
            and not worker.stopping
            and not self._selected(session_id)
            and not self._being_selected(session_id)
        ):
            worker.stopping = True
            worker.transition.clear()
            await worker.peer.send(("stop", worker.idle_revision))

    async def request(self, operation, session_id, arguments):
        assert not self.closed
        lock = self.locks.setdefault(session_id, asyncio.Lock())
        just_started = False
        while True:
            async with lock:
                if self.closed:
                    raise RuntimeError("Host closed")
                worker = self.workers.get(session_id)
                if worker is None:
                    worker = await self._start(session_id)
                    just_started = True
                if not worker.stopping:
                    worker.requests += 1
                    worker.idle_revision = None
                    break
            await worker.transition.wait()
        try:
            if just_started and operation != "apply_authorization_policy":
                await worker.peer.request(
                    "apply_authorization_policy",
                    session_id,
                    {
                        "preference": self.web_auto_authorize(session_id),
                        "grant": self._auto_grant(session_id),
                    },
                )
            return await worker.peer.request(operation, session_id, arguments)
        finally:
            worker.requests -= 1
            if not worker.exited.is_set():
                await self._stop_idle(session_id, worker)

    def web_auto_authorize(self, session_id):
        return self._auto_authorize.get(session_id)

    def is_paused(self, session_id):
        return self._pause.get(session_id)

    def control_approval(self, session_id):
        return self._control_approvals.get(session_id)

    def _owners_of(self, session_id):
        return tuple(
            owner
            for owner, selected in self.selections.items()
            if selected == session_id
        )

    def _auto_grant(self, session_id):
        return auto_grant_for_owners(
            self._owners_of(session_id),
            self.web_auto_authorize(session_id),
        )

    def _with_preference(self, view, session_id):
        return replace(
            view,
            auto_authorize=self.web_auto_authorize(session_id),
            paused=self.is_paused(session_id),
        )

    async def _push_authorization_policy(self, session_id):
        return await self.request(
            "apply_authorization_policy",
            session_id,
            {
                "preference": self.web_auto_authorize(session_id),
                "grant": self._auto_grant(session_id),
            },
        )

    def conversation_status(self, session_id):
        return self.compact.store.status(session_id)

    def activity(self, session_id):
        worker = self.workers.get(session_id)
        if worker is not None and worker.running:
            return "running"
        return "idle"

    async def _emit_session_activity(self, session_id, activity):
        if (
            self.session_activity_sink is None
            or self.compact.store.reader_job(session_id) is not None
        ):
            return
        emitted = self.session_activity_sink(session_id, activity)
        if isawaitable(emitted):
            await emitted

    async def _emit_session_failed(self, session_id, message):
        if (
            self.session_failed_sink is None
            or self.compact.store.reader_job(session_id) is not None
        ):
            return
        emitted = self.session_failed_sink(session_id, message)
        if isawaitable(emitted):
            await emitted

    async def bound_workspace_id(self, session_id: str) -> str:
        """会话自己的归属；派生子会话与压缩 reader 都继承它。"""
        events = await SqliteJournal(self.store.require(session_id)).snapshot(
            session_id
        )
        workspace_id = bound_workspace_id(events)
        if workspace_id is None:
            raise UnboundSessionError(session_id)
        return workspace_id

    async def create(self, session_id, workspace_id):
        async with self.locks.setdefault(session_id, asyncio.Lock()):
            self.workspaces.get(workspace_id)
            await self.store.create(session_id, workspace_id=workspace_id)

    async def fork_and_accept_input(
        self,
        owner,
        source_session_id,
        message_id,
        edited_text,
        *,
        child_session_id,
        delivery_id,
        source="user",
    ):
        async with self.locks.setdefault(source_session_id, asyncio.Lock()):
            original = await self.store.fork_before_message(
                source_session_id,
                message_id,
                child_session_id,
            )
        await self.select(owner, child_session_id)
        return await self.accept_input(
            child_session_id,
            edited_text,
            delivery_id=delivery_id,
            source=source,
            artifact_refs=original.artifact_refs,
        )

    async def select(self, owner, session_id):
        async with self.selection_locks.setdefault(owner, asyncio.Lock()):
            self.store.require(session_id)
            previous = self.selections.get(owner)
            self.selecting[owner] = session_id
            self.selections[owner] = session_id
            try:
                view = await self._select_view(session_id)
            except BaseException:
                if previous is None:
                    self.selections.pop(owner, None)
                else:
                    self.selections[owner] = previous
                raise
            finally:
                del self.selecting[owner]
                worker = self.workers.get(session_id)
                if worker is not None:
                    await self._stop_idle(session_id, worker)
            if previous is not None and previous != session_id:
                worker = self.workers.get(previous)
                if worker is not None:
                    await self._push_authorization_policy(previous)
                    await self._stop_idle(previous, worker)
            return view

    async def _journal_state(self, session_id):
        events = await SqliteJournal(self.store.require(session_id)).snapshot(
            session_id
        )
        return events, replay(session_id, events).state

    async def _project_view(self, session_id):
        events, state = await self._journal_state(session_id)
        return self._with_preference(
            session_view(
                state,
                control_approval=self.control_approval(session_id),
                has_active_subagents=bool(project_pending(events)),
            ),
            session_id,
        )

    async def _should_wake(self, session_id):
        if self.is_paused(session_id):
            return False
        _events, state = await self._journal_state(session_id)
        return session_view(state).should_wake

    async def _select_view(self, session_id):
        if await self._should_wake(session_id):
            if session_id in self.workers:
                await self._push_authorization_policy(session_id)
            return await self.resume(session_id)
        if session_id in self.workers:
            await self._push_authorization_policy(session_id)
        return await self._project_view(session_id)

    async def release(self, owner):
        async with self.selection_locks.setdefault(owner, asyncio.Lock()):
            session_id = self.selections.pop(owner, None)
            if session_id is None:
                return
            worker = self.workers.get(session_id)
            if worker is not None:
                await self._push_authorization_policy(session_id)
                await self._stop_idle(session_id, worker)

    async def resume(self, session_id):
        operation = "view" if self.is_paused(session_id) else "resume"
        return self._with_preference(
            await self.compact.application(operation, session_id, {}),
            session_id,
        )

    async def retry(self, session_id):
        if self.is_paused(session_id):
            return await self.set_paused(session_id, False)
        return await self.resume(session_id)

    async def view(self, session_id):
        return self._with_preference(
            await self.compact.application("view", session_id, {}),
            session_id,
        )

    async def receive_user_message(self, session_id, content, **kwargs):
        if self._pause.get(session_id):
            self._pause.set(session_id, False)
        await self.compact.application(
            "receive_user_message", session_id, dict(content=content, **kwargs)
        )

    async def accept_input(self, session_id, content, **kwargs):
        if self._pause.get(session_id):
            self._pause.set(session_id, False)
        view = await self.compact.application(
            "accept_input", session_id, dict(content=content, **kwargs)
        )
        return self._with_preference(view, session_id)

    async def resolve_authorization(self, session_id, command_id, *, approved):
        await self.compact.application(
            "resolve_authorization",
            session_id,
            dict(command_id=command_id, approved=approved),
        )

    async def resolve_authorizations(self, session_id, *, approved):
        await self.compact.application(
            "resolve_authorizations", session_id, dict(approved=approved)
        )

    async def set_auto_authorize(self, session_id, enabled):
        self._auto_authorize.set(session_id, enabled)
        view = await self._push_authorization_policy(session_id)
        return self._with_preference(view, session_id)

    async def set_paused(self, session_id, paused):
        self._pause.set(session_id, bool(paused))
        if paused:
            return self._with_preference(
                await self.compact.application("view", session_id, {}),
                session_id,
            )
        return await self.resume(session_id)

    async def resolve_control(self, session_id, *, approved):
        return await self.compact.application(
            "resolve_control", session_id, dict(approved=approved)
        )

    async def cancel_turn(self, session_id):
        return self._with_preference(
            await self.compact.application("cancel_turn", session_id, {}),
            session_id,
        )

    async def wait_quiescent(self, session_id):
        worker = self.workers.get(session_id)
        if worker is None:
            return await self.view(session_id)
        while True:
            worker.changed.clear()
            if worker.failure is not None:
                raise worker.failure
            if worker.idle_revision is not None and not worker.idle_has_active_subagents:
                return await self.view(session_id)
            if worker.exited.is_set():
                return await self.view(session_id)
            await worker.changed.wait()

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
