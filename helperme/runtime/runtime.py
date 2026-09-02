from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from helperme.runtime.dispatcher import Dispatcher, ToolBinding
from helperme.runtime.events import (
    CommandAuthorized,
    CommandRejected,
    DomainFactCommitted,
    Event,
    EventDraft,
    DeliveryIdentity,
    TerminationRequested,
    UserMessageReceived,
)
from helperme.runtime.journal.api import Journal, LeaseLostError, StepClaimRequest
from helperme.runtime.model import CanonicalState, RuntimeStatus, Step
from helperme.runtime.projections import (
    ReplayView,
    TraceView,
    project_trace,
    replay,
)
from helperme.runtime.state import StateProjector
from helperme.runtime.step import DecisionMaker, IdFactory, StepRunner, random_id


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    step: Step | None
    status: RuntimeStatus


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
    ) -> None:
        if step_lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        self._journal = journal
        self._id_factory = id_factory
        self._worker_id = id_factory("worker") if worker_id is None else worker_id
        self._step_lease_seconds = step_lease_seconds
        self.projector = StateProjector()
        self.step_runner = StepRunner(
            journal,
            self.projector,
            decision_maker,
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
            decision_on_outcome=binding.decision_on_outcome,
            requires_authorization=binding.requires_authorization,
        )

    async def create_session(self, session_id: str) -> bool:
        """Persist a Host-selected Session identity without creating an Event."""

        return await self._journal.create_session(session_id)

    async def session_exists(self, session_id: str) -> bool:
        return await self._journal.session_exists(session_id)

    async def _append_external(
        self,
        session_id: str,
        payload: UserMessageReceived | DomainFactCommitted,
        delivery: DeliveryIdentity,
        *,
        causation_id: str | None = None,
    ) -> Event:
        result = await self._journal.accept_delivery(
            EventDraft(
                event_id=self._id_factory("event"),
                session_id=session_id,
                payload=payload,
                occurred_at=datetime.now(timezone.utc),
                causation_id=causation_id,
                delivery=delivery,
            )
        )
        return result.event

    async def receive_domain_fact(
        self,
        session_id: str,
        fact_type: str,
        data: object,
        *,
        delivery_id: str,
        source: str,
        requests_decision: bool = False,
        causation_id: str | None = None,
    ) -> Event:
        """Accept a non-human external fact for an already selected Session."""

        return await self._append_external(
            session_id,
            DomainFactCommitted(
                fact_type,
                data,
                requests_decision=requests_decision,
            ),
            DeliveryIdentity(source, delivery_id),
            causation_id=causation_id,
        )

    async def snapshot(self, session_id: str) -> tuple[Event, ...]:
        return await self._journal.snapshot(session_id)

    async def receive_user_message(
        self,
        session_id: str,
        content: str,
        *,
        delivery_id: str,
        source: str = "user",
    ) -> Event:
        return await self._append_external(
            session_id,
            UserMessageReceived(content),
            DeliveryIdentity(source, delivery_id),
        )

    async def receive_termination(
        self,
        session_id: str,
        reason: str | None = None,
        *,
        delivery_id: str,
        source: str = "user",
    ) -> Event:
        result = await self._journal.accept_termination(
            EventDraft(
                event_id=self._id_factory("event"),
                session_id=session_id,
                payload=TerminationRequested(reason),
                occurred_at=datetime.now(timezone.utc),
                delivery=DeliveryIdentity(source, delivery_id),
            ),
            terminal_event_id=self._id_factory("event"),
        )
        return result.event

    async def finalize(self, session_id: str) -> Event | None:
        return await self._journal.finalize(
            session_id,
            self._id_factory("event"),
        )

    async def grant_command(
        self,
        session_id: str,
        command_id: str,
    ) -> Event | None:
        event = await self._journal.grant_command(
            EventDraft(
                event_id=self._id_factory("event"),
                session_id=session_id,
                payload=CommandAuthorized(command_id),
                occurred_at=datetime.now(timezone.utc),
            )
        )
        if event is not None:
            await self.dispatcher.start_pending(session_id)
        return event

    async def reject_command(
        self,
        session_id: str,
        command_id: str,
    ) -> Event | None:
        return await self._journal.reject_command(
            EventDraft(
                event_id=self._id_factory("event"),
                session_id=session_id,
                payload=CommandRejected(command_id),
                occurred_at=datetime.now(timezone.utc),
            )
        )

    async def advance(self, session_id: str) -> AdvanceResult:
        lock = self._step_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            events = await self._journal.snapshot(session_id)
            frame = self.projector.project(
                session_id,
                events,
            ).next_decision
            if frame is None:
                dispatch = await self.dispatcher.start_pending(session_id)
                return AdvanceResult(None, dispatch.status)
            request = StepClaimRequest(
                session_id=session_id,
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
                return AdvanceResult(None, RuntimeStatus.RUNNABLE)
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
                return AdvanceResult(None, RuntimeStatus.RUNNABLE)
            except BaseException:
                await self._journal.release_step(lease)
                raise
            finally:
                if not operation.done():
                    await _cancel_task(operation)
                await _stop_heartbeat(heartbeat)
            step = step_event.payload.step
            dispatch = await self.dispatcher.start_pending(session_id)
            return AdvanceResult(step, dispatch.status)

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

    async def state(self, session_id: str) -> CanonicalState:
        events = await self._journal.snapshot(session_id)
        fingerprint = self._event_fingerprint(events)
        journal_position = events[-1].sequence if events else 0
        checkpoint = await self._journal.load_checkpoint(
            session_id,
            journal_position,
            fingerprint,
        )
        if checkpoint is not None:
            return checkpoint
        state = self.projector.project(session_id, events).state
        await self._journal.save_checkpoint(state, fingerprint)
        return state

    async def status(self, session_id: str) -> RuntimeStatus:
        return (await self.state(session_id)).status

    async def trace(self, session_id: str) -> TraceView:
        events = await self._journal.snapshot(session_id)
        return project_trace(session_id, events)

    async def replay(self, session_id: str) -> ReplayView:
        events = await self._journal.snapshot(session_id)
        return replay(session_id, events)

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
