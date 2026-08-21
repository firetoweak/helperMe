from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from agent_runtime.events import (
    CommandOutcomeReceived,
    CommandRecoveryRequired,
    CommandReconcileStarted,
    DispatchAttemptConfirmedNoEffect,
    DispatchAttemptStarted,
    Event,
    EventDraft,
    ExternalOperationAccepted,
)
from agent_runtime.journal import Journal, LeaseLostError
from agent_runtime.model import (
    AttemptState,
    CancelTool,
    Command,
    CommandOutcome,
    CommandPhase,
    CommandState,
    InvokeTool,
    OutcomeStatus,
    RecoveryContract,
    RetrySemantics,
    RunningRecovery,
)
from agent_runtime.state import StateProjector
from agent_runtime.step import IdFactory, random_id


@dataclass(frozen=True, slots=True)
class AttemptContext:
    stream_id: str
    command_id: str
    attempt_id: str
    attempt_number: int
    idempotency_key: str | None


@dataclass(frozen=True, slots=True)
class ToolAccepted:
    external_operation_id: str


@dataclass(frozen=True, slots=True)
class ToolTerminal:
    outcome: CommandOutcome


@dataclass(frozen=True, slots=True)
class RecoveryNoEffect:
    pass


@dataclass(frozen=True, slots=True)
class RecoveryIndeterminate:
    reason: str = "indeterminate"


@dataclass(frozen=True, slots=True)
class RecoveryStillRunning:
    pass


ToolHandler = Callable[
    [AttemptContext, Mapping[str, object]],
    Awaitable[object],
]
UnknownReconciler = Callable[
    [AttemptContext, Mapping[str, object]],
    Awaitable[
        ToolAccepted
        | ToolTerminal
        | RecoveryNoEffect
        | RecoveryIndeterminate
    ],
]
RunningQuery = Callable[
    [AttemptContext, str],
    Awaitable[ToolTerminal | RecoveryStillRunning],
]


class CancellationContract(str, Enum):
    UNSUPPORTED = "unsupported"
    TASK_CANCELLED_ERROR_CONFIRMS = "task_cancelled_error_confirms"


@dataclass(frozen=True, slots=True)
class ToolBinding:
    handler: ToolHandler
    recovery: RecoveryContract = RecoveryContract()
    reconcile_unknown: UnknownReconciler | None = None
    query_running: RunningQuery | None = None
    cancellation: CancellationContract = CancellationContract.UNSUPPORTED

    def __post_init__(self) -> None:
        if self.recovery.reconcile_unknown != (
            self.reconcile_unknown is not None
        ):
            raise ValueError("unknown reconcile contract mismatch")
        requires_query = (
            self.recovery.running_recovery is RunningRecovery.QUERY
        )
        if requires_query != (self.query_running is not None):
            raise ValueError("running query contract mismatch")


@dataclass(frozen=True, slots=True)
class _NotApplicable:
    reason: str


@dataclass(frozen=True, slots=True)
class _OutcomeCommitted:
    event: Event


class Dispatcher:
    def __init__(
        self,
        journal: Journal,
        projector: StateProjector,
        bindings: Mapping[str, ToolBinding],
        id_factory: IdFactory = random_id,
        *,
        worker_id: str = "local",
        attempt_lease_seconds: float = 30.0,
        reconcile_lease_seconds: float = 30.0,
    ) -> None:
        if attempt_lease_seconds <= 0 or reconcile_lease_seconds <= 0:
            raise ValueError("dispatcher lease durations must be positive")
        self._journal = journal
        self._projector = projector
        self._bindings = dict(bindings)
        self._id_factory = id_factory
        self._worker_id = worker_id
        self._attempt_lease_seconds = attempt_lease_seconds
        self._reconcile_lease_seconds = reconcile_lease_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._executions: dict[str, asyncio.Task[object]] = {}
        self._cancel_requests: set[str] = set()

    async def start_pending(self, stream_id: str) -> tuple[str, ...]:
        events = await self._journal.snapshot(stream_id)
        state = self._projector.project(stream_id, events).state
        cancel_targets = {
            command_state.command.effect.target_command_id
            for command_state in state.commands
            if isinstance(command_state.command.effect, CancelTool)
            and command_state.phase is not CommandPhase.TERMINAL
            and not command_state.abandoned
        }
        pending = tuple(sorted((
            command_state
            for command_state in state.commands
            if command_state.phase is CommandPhase.PENDING
            and not command_state.abandoned
            and command_state.dispatch_eligible_by_event_id is not None
            and command_state.authorization_rejected_by_event_id is None
            and command_state.command.command_id not in cancel_targets
            and (
                command_state.command.command_id not in self._tasks
                or self._tasks[command_state.command.command_id].done()
            )
        ), key=lambda item: not isinstance(item.command.effect, CancelTool)))
        started: list[str] = []
        for command_state in pending:
            command = command_state.command
            if isinstance(command.effect, InvokeTool):
                self._binding_for(command)
            attempt_id = self._id_factory("attempt")
            dispatch_event = await self._journal.start_attempt(EventDraft(
                event_id=self._id_factory("event"),
                stream_id=stream_id,
                payload=DispatchAttemptStarted(
                    attempt_id=attempt_id,
                    command_id=command.command_id,
                    attempt_number=len(command_state.attempts) + 1,
                    claim_token=self._id_factory("attempt-claim"),
                    worker_id=self._worker_id,
                ),
                occurred_at=datetime.now(timezone.utc),
                causation_id=command_state.dispatch_eligible_by_event_id,
            ), lease_seconds=self._attempt_lease_seconds)
            if dispatch_event is None:
                continue
            if isinstance(command.effect, InvokeTool):
                self._executions[command.command_id] = asyncio.create_task(
                    self._invoke_tool(stream_id, command, dispatch_event),
                    name=f"agent-tool:{attempt_id}",
                )
            self._tasks[command.command_id] = asyncio.create_task(
                self._run(stream_id, command, dispatch_event),
                name=f"agent-command:{attempt_id}",
            )
            started.append(command.command_id)
        return tuple(started)

    async def recover_once(self, stream_id: str) -> tuple[str, ...]:
        touched: list[str] = []
        events = await self._journal.snapshot(stream_id)
        state = self._projector.project(stream_id, events).state
        events_by_id = {event.event_id: event for event in events}
        for command_state in state.commands:
            command_id = command_state.command.command_id
            if (
                command_state.abandoned
                or command_state.phase is CommandPhase.TERMINAL
                or not isinstance(command_state.command.effect, CancelTool)
            ):
                continue
            local_task = self._tasks.get(command_id)
            if local_task is not None and not local_task.done():
                continue
            if command_state.phase not in (
                CommandPhase.UNKNOWN,
                CommandPhase.RUNNING,
            ):
                continue
            attempt = self._current_attempt(command_state)
            event = await self._recover_cancel(
                stream_id,
                command_state,
                attempt,
                events_by_id[attempt.started_event_id],
            )
            if event is not None:
                touched.append(event.event_id)

        events = await self._journal.snapshot(stream_id)
        state = self._projector.project(stream_id, events).state
        for command_state in state.commands:
            command_id = command_state.command.command_id
            if (
                command_state.abandoned
                or command_state.phase is CommandPhase.TERMINAL
                or not isinstance(command_state.command.effect, InvokeTool)
            ):
                continue
            local_task = self._tasks.get(command_id)
            if local_task is not None and not local_task.done():
                continue
            if command_state.phase is CommandPhase.UNKNOWN:
                event = await self._recover_unknown(stream_id, command_state)
                if event is not None:
                    touched.append(event.event_id)
            elif command_state.phase is CommandPhase.RUNNING:
                event = await self._recover_running(stream_id, command_state)
                if event is not None:
                    touched.append(event.event_id)
        touched.extend(await self.start_pending(stream_id))
        return tuple(touched)

    async def wait(self, command_id: str) -> None:
        await asyncio.shield(self._tasks[command_id])

    async def wait_all(self) -> None:
        if self._tasks:
            await asyncio.gather(*(
                asyncio.shield(task)
                for task in self._tasks.values()
            ))

    async def _run(
        self,
        stream_id: str,
        command: Command,
        dispatch_event: Event,
    ) -> None:
        payload = dispatch_event.payload
        attempt_id = payload.attempt_id
        heartbeat = asyncio.create_task(
            self._attempt_heartbeat(attempt_id, payload.claim_token),
            name=f"agent-attempt-heartbeat:{attempt_id}",
        )
        try:
            try:
                result = await self._await_while_leased(
                    self._execute(stream_id, command, dispatch_event),
                    heartbeat,
                )
            except asyncio.CancelledError:
                if command.command_id not in self._cancel_requests:
                    raise
                await self._append_outcome(
                    stream_id,
                    command.command_id,
                    attempt_id,
                    CommandOutcome(OutcomeStatus.CANCELLED),
                    dispatch_event.event_id,
                )
                self._cancel_requests.discard(command.command_id)
                return

            if isinstance(result, ToolAccepted):
                await self._append_accepted(
                    stream_id,
                    command.command_id,
                    attempt_id,
                    result.external_operation_id,
                    dispatch_event.event_id,
                )
                return
            if isinstance(result, _OutcomeCommitted):
                return
            if isinstance(result, ToolTerminal):
                outcome = result.outcome
            elif isinstance(result, _NotApplicable):
                outcome = CommandOutcome(
                    OutcomeStatus.NOT_APPLICABLE,
                    value=result.reason,
                )
            else:
                outcome = CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)
            await self._append_outcome(
                stream_id,
                command.command_id,
                attempt_id,
                outcome,
                dispatch_event.event_id,
            )
            self._cancel_requests.discard(command.command_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._journal.release_attempt(
                attempt_id,
                payload.claim_token,
            )
            if isinstance(command.effect, CancelTool):
                await self.start_pending(stream_id)

    async def _execute(
        self,
        stream_id: str,
        command: Command,
        dispatch_event: Event,
    ) -> object:
        effect = command.effect
        if isinstance(effect, InvokeTool):
            return await self._executions[command.command_id]
        if isinstance(effect, CancelTool):
            return await self._cancel_target(
                stream_id,
                effect.target_command_id,
                dispatch_event,
            )
        raise TypeError(type(effect).__name__)

    async def _attempt_heartbeat(
        self,
        attempt_id: str,
        claim_token: str,
    ) -> None:
        interval = self._attempt_lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            renewed = await self._journal.renew_attempt(
                attempt_id,
                claim_token,
                lease_seconds=self._attempt_lease_seconds,
            )
            if not renewed:
                raise LeaseLostError(claim_token)

    async def _reconcile_heartbeat(self, reconcile_id: str) -> None:
        interval = self._reconcile_lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            renewed = await self._journal.renew_reconcile(
                reconcile_id,
                lease_seconds=self._reconcile_lease_seconds,
            )
            if not renewed:
                raise LeaseLostError(reconcile_id)

    @staticmethod
    async def _await_while_leased(
        operation: Awaitable[object],
        heartbeat: asyncio.Task[None],
    ) -> object:
        task = asyncio.create_task(operation)
        try:
            done, _ = await asyncio.wait(
                (task, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                error = heartbeat.exception()
                if error is None:
                    raise RuntimeError("lease heartbeat stopped")
                raise error
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _invoke_tool(
        self,
        stream_id: str,
        command: Command,
        dispatch_event: Event,
    ) -> object:
        effect = command.effect
        if not isinstance(effect, InvokeTool):
            raise TypeError(type(effect).__name__)
        binding = self._binding_for(command)
        return await binding.handler(
            self._attempt_context(stream_id, command, dispatch_event),
            effect.argument_dict(),
        )

    async def _recover_unknown(
        self,
        stream_id: str,
        state: CommandState,
    ) -> Event | None:
        command = state.command
        effect = command.effect
        if not isinstance(effect, InvokeTool):
            return None
        attempt = self._current_attempt(state)
        binding = self._binding_for(command)
        if binding.reconcile_unknown is None:
            return await self._ensure_recovery_required(
                stream_id,
                state,
                attempt,
                "reconcile_unavailable",
                attempt.started_event_id,
            )
        reconcile_event = await self._start_reconcile(
            stream_id,
            state,
            attempt,
        )
        if reconcile_event is None:
            return None
        reconcile_id = reconcile_event.payload.reconcile_id
        heartbeat = asyncio.create_task(
            self._reconcile_heartbeat(reconcile_id),
            name=f"agent-reconcile-heartbeat:{reconcile_id}",
        )
        try:
            result = await self._await_while_leased(
                binding.reconcile_unknown(
                    self._attempt_context_from_state(
                        stream_id,
                        state,
                        attempt,
                    ),
                    effect.argument_dict(),
                ),
                heartbeat,
            )
            return await self._apply_recovery_result(
                stream_id,
                state,
                attempt,
                result,
                reconcile_event,
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._journal.release_reconcile(reconcile_id)

    async def _recover_running(
        self,
        stream_id: str,
        state: CommandState,
    ) -> Event | None:
        command = state.command
        effect = command.effect
        if not isinstance(effect, InvokeTool):
            return None
        attempt = self._current_attempt(state)
        binding = self._binding_for(command)
        if (
            binding.recovery.running_recovery is not RunningRecovery.QUERY
        ):
            return None
        reconcile_event = await self._start_reconcile(
            stream_id,
            state,
            attempt,
        )
        if reconcile_event is None:
            return None
        reconcile_id = reconcile_event.payload.reconcile_id
        heartbeat = asyncio.create_task(
            self._reconcile_heartbeat(reconcile_id),
            name=f"agent-reconcile-heartbeat:{reconcile_id}",
        )
        try:
            result = await self._await_while_leased(
                binding.query_running(
                    self._attempt_context_from_state(
                        stream_id,
                        state,
                        attempt,
                    ),
                    attempt.external_operation_id,
                ),
                heartbeat,
            )
            if isinstance(result, RecoveryStillRunning):
                return reconcile_event
            if isinstance(result, ToolTerminal):
                return await self._append_outcome(
                    stream_id,
                    command.command_id,
                    attempt.attempt_id,
                    result.outcome,
                    reconcile_event.event_id,
                )
            raise TypeError(type(result).__name__)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._journal.release_reconcile(reconcile_id)

    async def _recover_cancel(
        self,
        stream_id: str,
        state: CommandState,
        attempt: AttemptState,
        dispatch_event: Event,
    ) -> Event | None:
        effect = state.command.effect
        if not isinstance(effect, CancelTool):
            raise TypeError(type(effect).__name__)
        reconcile_event = await self._start_reconcile(
            stream_id,
            state,
            attempt,
        )
        if reconcile_event is None:
            return None
        reconcile_id = reconcile_event.payload.reconcile_id
        heartbeat = asyncio.create_task(
            self._reconcile_heartbeat(reconcile_id),
            name=f"agent-reconcile-heartbeat:{reconcile_id}",
        )
        try:
            events = await self._journal.snapshot(stream_id)
            target = self._projector.project(
                stream_id,
                events,
            ).state.command(effect.target_command_id)
            target_task = self._tasks.get(effect.target_command_id)
            if (
                target.phase in (CommandPhase.UNKNOWN, CommandPhase.RUNNING)
                and (target_task is None or target_task.done())
            ):
                return await self._ensure_recovery_required(
                    stream_id,
                    state,
                    attempt,
                    "cancel_target_execution_unknown",
                    reconcile_event.event_id,
                )
            result = await self._cancel_target(
                stream_id,
                effect.target_command_id,
                dispatch_event,
                fact_causation_id=reconcile_event.event_id,
            )
            if isinstance(result, _OutcomeCommitted):
                return result.event
            outcome = (
                CommandOutcome(
                    OutcomeStatus.NOT_APPLICABLE,
                    value=result.reason,
                )
                if isinstance(result, _NotApplicable)
                else CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)
            )
            return await self._append_outcome(
                stream_id,
                state.command.command_id,
                attempt.attempt_id,
                outcome,
                reconcile_event.event_id,
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._journal.release_reconcile(reconcile_id)

    async def _apply_recovery_result(
        self,
        stream_id: str,
        state: CommandState,
        attempt: AttemptState,
        result: object,
        reconcile_event: Event,
    ) -> Event | None:
        command_id = state.command.command_id
        if isinstance(result, ToolTerminal):
            return await self._append_outcome(
                stream_id,
                command_id,
                attempt.attempt_id,
                result.outcome,
                reconcile_event.event_id,
            )
        if isinstance(result, ToolAccepted):
            return await self._append_accepted(
                stream_id,
                command_id,
                attempt.attempt_id,
                result.external_operation_id,
                reconcile_event.event_id,
            )
        if isinstance(result, RecoveryNoEffect):
            return await self._journal.confirm_no_effect(EventDraft(
                event_id=self._id_factory("event"),
                stream_id=stream_id,
                payload=DispatchAttemptConfirmedNoEffect(
                    command_id=command_id,
                    attempt_id=attempt.attempt_id,
                ),
                occurred_at=datetime.now(timezone.utc),
                causation_id=reconcile_event.event_id,
            ))
        if isinstance(result, RecoveryIndeterminate):
            return await self._ensure_recovery_required(
                stream_id,
                state,
                attempt,
                result.reason,
                reconcile_event.event_id,
            )
        raise TypeError(type(result).__name__)

    async def _start_reconcile(
        self,
        stream_id: str,
        state: CommandState,
        attempt: AttemptState,
    ) -> Event | None:
        cause = (
            attempt.accepted_event_id
            if attempt.phase.value == "running"
            else attempt.started_event_id
        )
        return await self._journal.start_reconcile(EventDraft(
            event_id=self._id_factory("event"),
            stream_id=stream_id,
            payload=CommandReconcileStarted(
                reconcile_id=self._id_factory("reconcile"),
                reconcile_number=attempt.reconcile_count + 1,
                command_id=state.command.command_id,
                attempt_id=attempt.attempt_id,
                worker_id=self._worker_id,
            ),
            occurred_at=datetime.now(timezone.utc),
            causation_id=cause,
        ), lease_seconds=self._reconcile_lease_seconds)

    async def _ensure_recovery_required(
        self,
        stream_id: str,
        state: CommandState,
        attempt: AttemptState,
        reason: str,
        causation_id: str,
    ) -> Event | None:
        actions = ["abandon", "request_user"]
        if (
            state.command.recovery.retry_semantics
            is not RetrySemantics.PROHIBITED
        ):
            actions.insert(0, "retry")
        result = await self._journal.ensure_recovery_required(EventDraft(
            event_id=self._id_factory("event"),
            stream_id=stream_id,
            payload=CommandRecoveryRequired(
                command_id=state.command.command_id,
                attempt_id=attempt.attempt_id,
                reason=reason,
                allowed_actions=tuple(actions),
            ),
            occurred_at=datetime.now(timezone.utc),
            causation_id=causation_id,
        ))
        return result.event if result is not None else None

    async def _cancel_target(
        self,
        stream_id: str,
        target_command_id: str,
        dispatch_event: Event,
        *,
        fact_causation_id: str | None = None,
    ) -> object:
        events = await self._journal.snapshot(stream_id)
        target = self._projector.project(
            stream_id,
            events,
        ).state.command(target_command_id)
        if target.phase is CommandPhase.TERMINAL:
            canonical = next(
                event
                for event in events
                if event.event_id == target.canonical_outcome_event_id
            )
            cause = next((
                event
                for event in events
                if event.event_id == canonical.causation_id
            ), None)
            caused_by_cancel_attempt = (
                canonical.causation_id == dispatch_event.event_id
                or (
                    cause is not None
                    and isinstance(cause.payload, CommandReconcileStarted)
                    and cause.payload.command_id
                    == dispatch_event.payload.command_id
                    and cause.payload.attempt_id
                    == dispatch_event.payload.attempt_id
                )
            )
            if (
                target.outcome.status is OutcomeStatus.CANCELLED
                and (
                    caused_by_cancel_attempt
                    or canonical.sequence > dispatch_event.sequence
                )
            ):
                return "cancelled"
            return _NotApplicable("already_terminal")
        if target.phase is CommandPhase.PENDING:
            cause = fact_causation_id or dispatch_event.event_id
            committed = await self._journal.commit_pending_cancellation(
                EventDraft(
                    event_id=self._id_factory("event"),
                    stream_id=stream_id,
                    payload=CommandOutcomeReceived(
                        command_id=target_command_id,
                        attempt_id=None,
                        outcome=CommandOutcome(OutcomeStatus.CANCELLED),
                    ),
                    occurred_at=datetime.now(timezone.utc),
                    causation_id=cause,
                ),
                EventDraft(
                    event_id=self._id_factory("event"),
                    stream_id=stream_id,
                    payload=CommandOutcomeReceived(
                        command_id=dispatch_event.payload.command_id,
                        attempt_id=dispatch_event.payload.attempt_id,
                        outcome=CommandOutcome(
                            OutcomeStatus.SUCCEEDED,
                            value="cancelled",
                        ),
                    ),
                    occurred_at=datetime.now(timezone.utc),
                    causation_id=cause,
                ),
            )
            if committed is not None:
                return _OutcomeCommitted(committed[1])
            events = await self._journal.snapshot(stream_id)
            target = self._projector.project(
                stream_id,
                events,
            ).state.command(target_command_id)
            if target.phase is CommandPhase.PENDING:
                raise LeaseLostError(dispatch_event.payload.claim_token)
            if target.phase is CommandPhase.TERMINAL:
                canonical = next(
                    event
                    for event in events
                    if event.event_id == target.canonical_outcome_event_id
                )
                if (
                    target.outcome.status is OutcomeStatus.CANCELLED
                    and (
                        canonical.causation_id == cause
                        or canonical.sequence > dispatch_event.sequence
                    )
                ):
                    return "cancelled"
                return _NotApplicable("already_terminal")
        target_effect = target.command.effect
        if not isinstance(target_effect, InvokeTool):
            return _NotApplicable("cancellation_unsupported")
        binding = self._binding_for(target.command)
        if binding.cancellation is CancellationContract.UNSUPPORTED:
            return _NotApplicable("cancellation_unsupported")
        task = self._tasks.get(target_command_id)
        execution = self._executions.get(target_command_id)
        if task is None or execution is None or task.done():
            return _NotApplicable("execution_not_cancellable")
        self._cancel_requests.add(target_command_id)
        execution.cancel()
        await asyncio.gather(task, return_exceptions=True)
        events = await self._journal.snapshot(stream_id)
        target = self._projector.project(
            stream_id,
            events,
        ).state.command(target_command_id)
        if target.phase is not CommandPhase.TERMINAL:
            return _NotApplicable("cancellation_unconfirmed")
        if target.outcome.status is OutcomeStatus.CANCELLED:
            return "cancelled"
        return _NotApplicable(f"target_{target.outcome.status.value}")

    async def _append_accepted(
        self,
        stream_id: str,
        command_id: str,
        attempt_id: str,
        external_operation_id: str,
        causation_id: str,
    ) -> Event | None:
        return await self._journal.record_attempt_fact(EventDraft(
            event_id=self._id_factory("event"),
            stream_id=stream_id,
            payload=ExternalOperationAccepted(
                command_id=command_id,
                attempt_id=attempt_id,
                external_operation_id=external_operation_id,
            ),
            occurred_at=datetime.now(timezone.utc),
            causation_id=causation_id,
        ))

    async def _append_outcome(
        self,
        stream_id: str,
        command_id: str,
        attempt_id: str | None,
        outcome: CommandOutcome,
        causation_id: str,
    ) -> Event | None:
        draft = EventDraft(
            event_id=self._id_factory("event"),
            stream_id=stream_id,
            payload=CommandOutcomeReceived(
                command_id=command_id,
                attempt_id=attempt_id,
                outcome=outcome,
            ),
            occurred_at=datetime.now(timezone.utc),
            causation_id=causation_id,
        )
        if attempt_id is None:
            return await self._journal.append(draft)
        return await self._journal.record_attempt_fact(draft)

    @staticmethod
    def _current_attempt(state: CommandState) -> AttemptState:
        attempt = state.current_attempt
        if attempt is None:
            raise RuntimeError(
                f"command has no current attempt: {state.command.command_id}"
            )
        return attempt

    @staticmethod
    def _attempt_context(
        stream_id: str,
        command: Command,
        dispatch_event: Event,
    ) -> AttemptContext:
        payload = dispatch_event.payload
        return AttemptContext(
            stream_id=stream_id,
            command_id=command.command_id,
            attempt_id=payload.attempt_id,
            attempt_number=payload.attempt_number,
            idempotency_key=command.idempotency_key,
        )

    @staticmethod
    def _attempt_context_from_state(
        stream_id: str,
        state: CommandState,
        attempt: AttemptState,
    ) -> AttemptContext:
        return AttemptContext(
            stream_id=stream_id,
            command_id=state.command.command_id,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            idempotency_key=state.command.idempotency_key,
        )

    def _binding_for(self, command: Command) -> ToolBinding:
        effect = command.effect
        if not isinstance(effect, InvokeTool):
            raise TypeError(type(effect).__name__)
        binding = self._bindings[effect.name]
        if binding.recovery != command.recovery:
            raise ValueError(
                f"tool recovery contract changed: {effect.name}"
            )
        return binding
