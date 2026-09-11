from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path
import threading

from helperme.assistant.assembly import build_assistant_assembly
from helperme.assistant.ipc import PipePeer, ProcessFailure
from helperme.assistant.subagent import (
    project_parent, record_interrupted_return, record_unexpected_return,
)
from helperme.paths import HelperMeHome
from helperme.runtime import SqliteJournal


async def run_worker(connection, session_id, path, config_factory, home_root):
    journal = SqliteJournal(path)
    try:
        await _run_session(connection, session_id, journal, config_factory, home_root)
    except Exception as error:
        try:
            returned = await record_unexpected_return(journal, session_id, error)
            if returned is not None:
                # One-way handoff: no dependency on the failed Worker's reader.
                await asyncio.to_thread(connection.send, ("return", *returned))
        except Exception as reporting_error:
            raise ExceptionGroup(
                "Worker failure and return handoff failure", [error, reporting_error]
            ) from None
        raise


async def _run_session(connection, session_id, journal, config_factory, home_root):
    # Each process owns all clients, caches and its single Journal.
    home = HelperMeHome(Path(home_root))
    await journal.prepare_recovery(session_id)
    await record_interrupted_return(journal, session_id)
    config = config_factory()
    events = await journal.snapshot(session_id)
    stop = asyncio.Event()
    active_requests = 0
    ready = asyncio.Event()
    revision = 0
    advertised = -1

    async def handle(operation, target, arguments):
        nonlocal revision, active_requests
        assert target == session_id
        await ready.wait()
        active_requests += 1
        revision += 1
        try:
            if operation == "compact_publish":
                return await assembly.compact.publish(arguments)
            if operation == "compact_snapshot":
                return await assembly.compact.snapshot(**arguments)
            if operation == "compact_ready":
                await assembly.scheduler.wake(session_id)
                return None
            if operation == "fact":
                await assembly.runtime.receive_domain_fact(session_id, **arguments)
                parent = project_parent(await journal.snapshot(session_id))
                if parent is not None:
                    assembly.subagents._parents[session_id] = parent
                await assembly.subagents.refresh_activity(session_id)
                if await assembly.subagents.note_returned(session_id):
                    return None
                await assembly.scheduler.wake(session_id)
                result = None
            elif operation == "create":
                result = await assembly.sessions.view(session_id)
            else:
                result = await getattr(assembly.sessions, operation)(
                    session_id, **arguments
                )
            return result
        finally:
            active_requests -= 1
            assembly.scheduler.changed.set()

    async def signal(kind, *payload):
        nonlocal advertised
        if kind != "stop":
            raise ValueError(f"Unknown Worker signal: {kind}")
        (observed_revision,) = payload
        if (
            observed_revision == revision
            and active_requests == 0
            and assembly.scheduler.idle
        ):
            stop.set()
        else:
            advertised = -1
            await peer.send(("busy",))
            assembly.scheduler.changed.set()

    peer = PipePeer(connection, handle, signal)

    async def sink(target, text):
        await peer.request("output", target, {"text": text})

    # These callbacks only affect display; queue their IPC in the same event loop.
    notifications: set[asyncio.Task] = set()

    def notify(kind, *values):
        task = asyncio.create_task(peer.send((kind, *values)))
        notifications.add(task)

        def completed(done):
            notifications.discard(done)
            if not done.cancelled() and done.exception() is not None:
                peer.failure = done.exception()

        task.add_done_callback(completed)

    assembly = await build_assistant_assembly(
        config,
        sink,
        journal,
        session_id=session_id,
        context_usage_sink=lambda *values: notify("usage", *values),
        subagent_activity_sink=lambda *values: notify("activity", *values),
        session_transport=peer.request,
        home=home,
    )
    async with config.llm, assembly.mcp.client_manager:
        reader = asyncio.create_task(peer.run())
        stopped = asyncio.create_task(stop.wait())
        try:
            # Rebuild only this Session. An explicit resume separately resumes its children.
            from helperme.assistant.runner import resume_session

            await resume_session(
                assembly.runtime,
                assembly.surface,
                session_id,
                assembly.sessions._management,
            )
            parent = project_parent(events)
            if parent is not None:
                assembly.subagents._parents[session_id] = parent
            ready.set()
            while not stop.is_set():
                changed = asyncio.create_task(assembly.scheduler.changed.wait())
                done, _ = await asyncio.wait(
                    (reader, stopped, changed), return_when=asyncio.FIRST_COMPLETED
                )
                if not changed.done():
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
                if reader in done:
                    await reader
                if assembly.scheduler._failure is not None:
                    raise assembly.scheduler._failure
                assembly.scheduler.changed.clear()
                if (
                    assembly.scheduler.idle
                    and active_requests == 0
                    and advertised != revision
                ):
                    view = await assembly.sessions.view(session_id)
                    # Control proposals currently live in this Worker until resolved.
                    if view.control_approval is None:
                        advertised = revision
                        await peer.send(("idle", revision, view.terminal))
            await peer.send(("stopping",))
        finally:
            await peer.close(RuntimeError("Worker closed"))
            reader.cancel()
            stopped.cancel()
            await asyncio.gather(reader, stopped, return_exceptions=True)
            await assembly.scheduler.close()
            if notifications:
                await asyncio.gather(*notifications)


def worker_main(connection, session_id, path, config_factory, home_root, admitted):
    def exit_with_parent():
        multiprocessing.parent_process().join()
        os._exit(1)

    threading.Thread(target=exit_with_parent, daemon=True).start()
    try:
        admitted.wait()
        asyncio.run(run_worker(connection, session_id, path, config_factory, home_root))
    except BaseException as error:
        # Process boundary: transport original diagnostics, then let the process fail.
        connection.send(("failure", ProcessFailure.capture(error)))
        raise
    finally:
        connection.close()
