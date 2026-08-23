from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256

from helperme.runtime.dispatcher import Dispatcher, ToolBinding
from helperme.runtime.events import (
    CommandAuthorized,
    CommandRejected,
    Event,
    EventDraft,
    DeliveryIdentity,
    EventPayload,
    TerminationRequested,
    UserInterruptReceived,
    UserMessageReceived,
)
from helperme.runtime.journal.api import Journal, LeaseLostError, StepClaimRequest
from helperme.runtime.model import CanonicalState, RuntimeStatus, Step
from helperme.runtime.projections import (
    ReplayView,
    TraceView,
    TurnView,
    project_trace,
    project_turn,
    replay,
)
from helperme.runtime.state import StateProjector
from helperme.runtime.step import DecisionMaker, IdFactory, StepRunner, random_id


async def _cancel_task(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _stop_heartbeat(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, LeaseLostError):
        pass


class AgentRuntime:
    def __init__(
        self,
        journal: Journal,
        decision_maker: DecisionMaker,
        tool_bindings: Mapping[str, ToolBinding],
        id_factory: IdFactory = random_id,
        *,
        worker_id: str | None = None,
        step_lease_seconds: float = 30.0,
        attempt_lease_seconds: float = 30.0,
        reconcile_lease_seconds: float = 30.0,
    ) -> None:
        if step_lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        self._journal = journal
        self._id_factory = id_factory
        self._worker_id = (
            id_factory("worker") if worker_id is None else worker_id
        )
        self._step_lease_seconds = step_lease_seconds
        self.projector = StateProjector()
        self.step_runner = StepRunner(
            journal,
            self.projector,
            decision_maker,
            {
                name: binding.recovery
                for name, binding in tool_bindings.items()
            },
            id_factory,
            requires_authorization={
                name: binding.requires_authorization
                for name, binding in tool_bindings.items()
            },
            decision_on_outcome={
                name: binding.decision_on_outcome
                for name, binding in tool_bindings.items()
            },
        )
        self.dispatcher = Dispatcher(
            journal,
            self.projector,
            tool_bindings,
            id_factory,
            worker_id=self._worker_id,
            attempt_lease_seconds=attempt_lease_seconds,
            reconcile_lease_seconds=reconcile_lease_seconds,
        )
        self._step_locks: dict[str, asyncio.Lock] = {}

    def bind_tool(
        self,
        name: str,
        binding: ToolBinding,
    ) -> None:
        self.dispatcher.bind(name, binding)
        self.step_runner.bind(
            name,
            recovery=binding.recovery,
            decision_on_outcome=binding.decision_on_outcome,
            requires_authorization=binding.requires_authorization,
        )

    async def create_stream(self, stream_id: str) -> bool:
        """Persist a Host-selected Stream identity without creating an Event."""

        return await self._journal.create_stream(stream_id)

    async def stream_exists(self, stream_id: str) -> bool:
        return await self._journal.stream_exists(stream_id)

    async def _append_external(
        self,
        stream_id: str,
        payload: UserMessageReceived | UserInterruptReceived,
        delivery: DeliveryIdentity,
    ) -> Event:
        result = await self._journal.accept_delivery(EventDraft(
            event_id=self._id_factory("event"),
            stream_id=stream_id,
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
            delivery=delivery,
        ))
        return result.event

    async def record_fact(
        self,
        stream_id: str,
        payload: EventPayload,
        *,
        causation_id: str | None = None,
    ) -> Event:
        return await self._journal.append(EventDraft(
            event_id=self._id_factory("event"),
            stream_id=stream_id,
            payload=payload,
            occurred_at=datetime.now(timezone.utc),
            causation_id=causation_id,
        ))

    async def snapshot(self, stream_id: str) -> tuple[Event, ...]:
        return await self._journal.snapshot(stream_id)

    async def receive_user_message(
        self,
        stream_id: str,
        content: str,
        *,
        delivery_id: str,
        source: str = "user",
    ) -> Event:
        return await self._append_external(
            stream_id,
            UserMessageReceived(content),
            DeliveryIdentity(source, delivery_id),
        )

    async def receive_interrupt(
        self,
        stream_id: str,
        reason: str | None = None,
        *,
        delivery_id: str,
        source: str = "user",
    ) -> Event:
        return await self._append_external(
            stream_id,
            UserInterruptReceived(reason),
            DeliveryIdentity(source, delivery_id),
        )

    async def receive_termination(
        self,
        stream_id: str,
        reason: str | None = None,
        *,
        delivery_id: str,
        source: str = "user",
    ) -> Event:
        result = await self._journal.accept_termination(
            EventDraft(
                event_id=self._id_factory("event"),
                stream_id=stream_id,
                payload=TerminationRequested(reason),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity(source, delivery_id),
            ),
            terminal_event_id=self._id_factory("event"),
        )
        return result.event

    async def finalize(self, stream_id: str) -> Event | None:
        return await self._journal.finalize(
            stream_id,
            self._id_factory("event"),
        )

    async def grant_command(
        self,
        stream_id: str,
        command_id: str,
    ) -> Event | None:
        events = await self._journal.snapshot(stream_id)
        command_state = self.projector.project(
            stream_id,
            events,
        ).state.command(command_id)
        event = await self._journal.grant_command(EventDraft(
            event_id=self._id_factory("event"),
            stream_id=stream_id,
            payload=CommandAuthorized(command_id),
            occurred_at=datetime.now(timezone.utc),
            causation_id=command_state.issued_by_event_id,
        ))
        if event is not None:
            await self.dispatcher.start_pending(stream_id)
        return event

    async def reject_command(
        self,
        stream_id: str,
        command_id: str,
    ) -> Event | None:
        events = await self._journal.snapshot(stream_id)
        command_state = self.projector.project(
            stream_id,
            events,
        ).state.command(command_id)
        return await self._journal.reject_command(EventDraft(
            event_id=self._id_factory("event"),
            stream_id=stream_id,
            payload=CommandRejected(command_id),
            occurred_at=datetime.now(timezone.utc),
            causation_id=command_state.issued_by_event_id,
        ))

    async def advance(self, stream_id: str) -> Step | None:
        lock = self._step_locks.setdefault(stream_id, asyncio.Lock())
        async with lock:
            events = await self._journal.snapshot(stream_id)
            frame = self.projector.project(
                stream_id,
                events,
            ).next_decision
            if frame is None:
                await self.dispatcher.start_pending(stream_id)
                return None
            request = StepClaimRequest(
                stream_id=stream_id,
                trigger_event_id=frame.trigger_event.event_id,
                decision_cursor=frame.decision_cursor,
                basis_state_version=frame.basis_state_version,
                observed_journal_position=frame.observed_journal_position,
            )
            lease = await self._journal.acquire_step(
                request,
                token=self._id_factory("step-claim"),
                owner_id=self._worker_id,
                lease_seconds=self._step_lease_seconds,
            )
            if lease is None:
                return None
            heartbeat = asyncio.create_task(
                self._step_heartbeat(lease),
                name=f"agent-step-heartbeat:{lease.token}",
            )
            operation = asyncio.create_task(
                self.step_runner.commit(frame, lease),
                name=f"agent-step:{lease.token}",
            )
            try:
                done, _ = await asyncio.wait(
                    (operation, heartbeat),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if operation in done:
                    step_event = await operation
                else:
                    error = heartbeat.exception()
                    if error is None:
                        raise RuntimeError("step lease heartbeat stopped")
                    raise error
            except LeaseLostError:
                await self._journal.release_step(lease)
                return None
            except BaseException:
                await self._journal.release_step(lease)
                raise
            finally:
                if not operation.done():
                    await _cancel_task(operation)
                await _stop_heartbeat(heartbeat)
            step = step_event.payload.step
            await self.dispatcher.start_pending(stream_id)
            return step

    async def _step_heartbeat(self, lease) -> None:
        interval = self._step_lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            renewed = await self._journal.renew_step(
                lease,
                lease_seconds=self._step_lease_seconds,
            )
            if not renewed:
                raise LeaseLostError(lease.token)

    async def state(self, stream_id: str) -> CanonicalState:
        events = await self._journal.snapshot(stream_id)
        fingerprint = self._event_fingerprint(events)
        journal_position = events[-1].sequence if events else 0
        checkpoint = await self._journal.load_checkpoint(
            stream_id,
            journal_position,
            fingerprint,
        )
        if checkpoint is not None:
            return checkpoint
        state = self.projector.project(stream_id, events).state
        await self._journal.save_checkpoint(state, fingerprint)
        return state

    async def recover_once(self, stream_id: str) -> tuple[str, ...]:
        return await self.dispatcher.recover_once(stream_id)

    async def status(self, stream_id: str) -> RuntimeStatus:
        return (await self.state(stream_id)).status

    async def turn(self, stream_id: str) -> TurnView:
        events = await self._journal.snapshot(stream_id)
        return project_turn(stream_id, events, self.projector)

    async def trace(self, stream_id: str) -> TraceView:
        events = await self._journal.snapshot(stream_id)
        return project_trace(stream_id, events)

    async def replay(self, stream_id: str) -> ReplayView:
        events = await self._journal.snapshot(stream_id)
        return replay(stream_id, events)

    @staticmethod
    def _event_fingerprint(events: tuple[Event, ...]) -> str:
        content = json.dumps(
            [
                [event.sequence, event.event_id, event.schema_version]
                for event in events
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return sha256(content.encode("utf-8")).hexdigest()
