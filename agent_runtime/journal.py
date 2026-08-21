from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol

from agent_runtime.codec import EVENT_SCHEMA_VERSION, delivery_fingerprint
from agent_runtime.events import (
    CommandOutcomeReceived,
    CommandRecoveryRequired,
    CommandReconcileStarted,
    DeliveryIdentity,
    DispatchAttemptConfirmedNoEffect,
    DispatchAttemptStarted,
    Event,
    EventDraft,
    ExternalOperationAccepted,
    StepCommitted,
    UserInterruptReceived,
    UserMessageReceived,
)
from agent_runtime.model import (
    CancelTool,
    CanonicalState,
    Command,
    InvokeTool,
    OutcomeStatus,
)


class DeliveryConflictError(ValueError):
    pass


class LeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppendResult:
    event: Event
    inserted: bool


@dataclass(frozen=True, slots=True)
class StepClaimRequest:
    stream_id: str
    trigger_event_id: str
    decision_cursor: int
    basis_state_version: str
    observed_journal_position: int


@dataclass(frozen=True, slots=True)
class StepLease:
    request: StepClaimRequest
    token: str
    owner_id: str
    generation: int
    expires_at: float


class Journal(Protocol):
    async def append(self, draft: EventDraft) -> Event:
        ...

    async def accept_delivery(self, draft: EventDraft) -> AppendResult:
        ...

    async def snapshot(self, stream_id: str) -> tuple[Event, ...]:
        ...

    async def acquire_step(
        self,
        request: StepClaimRequest,
        *,
        token: str,
        owner_id: str,
        lease_seconds: float,
    ) -> StepLease | None:
        ...

    async def release_step(self, lease: StepLease) -> None:
        ...

    async def renew_step(
        self,
        lease: StepLease,
        *,
        lease_seconds: float,
    ) -> bool:
        ...

    async def commit_step(
        self,
        lease: StepLease,
        draft: EventDraft,
    ) -> Event:
        ...

    async def start_attempt(
        self,
        draft: EventDraft,
        *,
        lease_seconds: float = 30.0,
    ) -> Event | None:
        ...

    async def renew_attempt(
        self,
        attempt_id: str,
        claim_token: str,
        *,
        lease_seconds: float,
    ) -> bool:
        ...

    async def release_attempt(
        self,
        attempt_id: str,
        claim_token: str,
    ) -> None:
        ...

    async def start_reconcile(
        self,
        draft: EventDraft,
        *,
        lease_seconds: float = 30.0,
    ) -> Event | None:
        ...

    async def renew_reconcile(
        self,
        reconcile_id: str,
        *,
        lease_seconds: float,
    ) -> bool:
        ...

    async def release_reconcile(self, reconcile_id: str) -> None:
        ...

    async def confirm_no_effect(
        self,
        draft: EventDraft,
    ) -> Event | None:
        ...

    async def record_attempt_fact(
        self,
        draft: EventDraft,
    ) -> Event | None:
        ...

    async def commit_pending_cancellation(
        self,
        target_draft: EventDraft,
        cancel_draft: EventDraft,
    ) -> tuple[Event, Event] | None:
        ...

    async def ensure_recovery_required(
        self,
        draft: EventDraft,
    ) -> AppendResult | None:
        ...

    async def load_checkpoint(
        self,
        stream_id: str,
        journal_position: int,
        fingerprint: str,
    ) -> CanonicalState | None:
        ...

    async def save_checkpoint(
        self,
        state: CanonicalState,
        fingerprint: str,
    ) -> None:
        ...

    async def delete_checkpoint(self, stream_id: str) -> None:
        ...


class MemoryJournal:
    def __init__(
        self,
        events: Iterable[Event] = (),
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._events: dict[str, list[Event]] = {}
        self._event_ids: dict[str, Event] = {}
        self._deliveries: dict[
            DeliveryIdentity,
            tuple[Event, str],
        ] = {}
        self._step_claims: dict[str, StepLease] = {}
        self._step_claim_tokens: dict[str, str] = {}
        self._step_consumptions: dict[str, str] = {}
        self._step_ids: set[str] = set()
        self._commands: dict[str, tuple[str, str]] = {}
        self._command_definitions: dict[str, Command] = {}
        self._abandoned_commands: set[str] = set()
        self._dispatch_eligibility: dict[str, str] = {}
        self._attempts: dict[tuple[str, int], Event] = {}
        self._attempt_ids: dict[str, Event] = {}
        self._attempt_claim_tokens: dict[str, Event] = {}
        self._attempt_leases: dict[str, tuple[str, float]] = {}
        self._attempt_terminal_events: dict[str, Event] = {}
        self._reconciles: dict[tuple[str, int], Event] = {}
        self._reconcile_ids: dict[str, Event] = {}
        self._reconcile_event_ids: dict[str, str] = {}
        self._reconcile_leases: dict[str, tuple[str, float]] = {}
        self._external_operations: dict[str, Event] = {}
        self._recovery_required: dict[tuple[str, str], Event] = {}
        self._terminal_commands: set[str] = set()
        self._checkpoints: dict[str, tuple[int, str, CanonicalState]] = {}
        self._clock = clock
        self._lock = asyncio.Lock()
        for event in events:
            self._restore(event)

    def _restore(self, event: Event) -> None:
        if type(event) is not Event:
            raise TypeError("restored event must be Event")
        if event.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported event schema version: {event.schema_version}"
            )
        is_external = isinstance(
            event.payload,
            (UserMessageReceived, UserInterruptReceived),
        )
        if is_external != (event.delivery is not None):
            raise ValueError("restored event delivery boundary is invalid")
        if event.event_id in self._event_ids:
            raise ValueError(f"duplicate event id: {event.event_id}")
        stream_events = self._events.setdefault(event.stream_id, [])
        expected = len(stream_events) + 1
        if event.sequence != expected:
            raise ValueError(
                f"invalid sequence for {event.stream_id}: "
                f"expected {expected}, got {event.sequence}"
            )
        if event.delivery is not None:
            existing = self._deliveries.get(event.delivery)
            if existing is not None:
                raise ValueError(f"duplicate delivery: {event.delivery}")
        self._prevalidate_index(event)
        stream_events.append(event)
        self._event_ids[event.event_id] = event
        self._index_event(event)
        if event.delivery is not None:
            self._deliveries[event.delivery] = (
                event,
                self._event_delivery_fingerprint(event),
            )

    async def append(self, draft: EventDraft) -> Event:
        self._validate_generic_append(draft)
        async with self._lock:
            return self._append_locked(draft).event

    async def accept_delivery(self, draft: EventDraft) -> AppendResult:
        self._validate_external_delivery(draft)
        if draft.delivery is None:
            raise ValueError("external event requires delivery identity")
        async with self._lock:
            receipt = self._deliveries.get(draft.delivery)
            if receipt is not None:
                existing, fingerprint = receipt
                if fingerprint != delivery_fingerprint(draft):
                    raise DeliveryConflictError(
                        f"delivery content conflict: {draft.delivery}"
                    )
                return AppendResult(existing, False)
            result = self._append_locked(draft)
            self._deliveries[draft.delivery] = (
                result.event,
                delivery_fingerprint(draft),
            )
            return result

    async def snapshot(self, stream_id: str) -> tuple[Event, ...]:
        async with self._lock:
            return tuple(self._events.get(stream_id, ()))

    async def acquire_step(
        self,
        request: StepClaimRequest,
        *,
        token: str,
        owner_id: str,
        lease_seconds: float,
    ) -> StepLease | None:
        if not token or not owner_id:
            raise ValueError("step claim identity must not be empty")
        if lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        async with self._lock:
            trigger = self._event_ids.get(request.trigger_event_id)
            if trigger is None or trigger.stream_id != request.stream_id:
                raise KeyError(request.trigger_event_id)
            if request.trigger_event_id in self._step_consumptions:
                return None
            current = self._step_claims.get(request.stream_id)
            now = self._clock()
            if current is not None and current.expires_at > now:
                if current.token == token and current.request == request:
                    return current
                return None
            token_stream = self._step_claim_tokens.get(token)
            if token_stream is not None and token_stream != request.stream_id:
                raise ValueError(f"duplicate step claim token: {token}")
            if current is not None:
                if self._step_claim_tokens.get(
                    current.token
                ) == request.stream_id:
                    self._step_claim_tokens.pop(current.token, None)
            generation = current.generation + 1 if current is not None else 1
            lease = StepLease(
                request=request,
                token=token,
                owner_id=owner_id,
                generation=generation,
                expires_at=now + lease_seconds,
            )
            self._step_claims[request.stream_id] = lease
            self._step_claim_tokens[token] = request.stream_id
            return lease

    async def release_step(self, lease: StepLease) -> None:
        async with self._lock:
            current = self._step_claims.get(lease.request.stream_id)
            if self._same_step_lease_identity(current, lease):
                del self._step_claims[lease.request.stream_id]
                if self._step_claim_tokens.get(
                    lease.token
                ) == lease.request.stream_id:
                    self._step_claim_tokens.pop(lease.token, None)

    async def renew_step(
        self,
        lease: StepLease,
        *,
        lease_seconds: float,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("step lease duration must be positive")
        async with self._lock:
            current = self._step_claims.get(lease.request.stream_id)
            now = self._clock()
            if (
                not self._same_step_lease_identity(current, lease)
                or current.expires_at <= now
            ):
                return False
            self._step_claims[lease.request.stream_id] = replace(
                current,
                expires_at=now + lease_seconds,
            )
            return True

    async def commit_step(
        self,
        lease: StepLease,
        draft: EventDraft,
    ) -> Event:
        self._validate_internal_draft(draft)
        async with self._lock:
            existing = self._event_ids.get(draft.event_id)
            if existing is not None:
                if not self._same_event(existing, draft):
                    raise ValueError(f"event id conflict: {draft.event_id}")
                if self._step_consumptions.get(
                    lease.request.trigger_event_id
                ) != existing.event_id:
                    raise LeaseLostError(lease.token)
                return existing
            self._validate_step_lease(lease, draft)
            event = self._append_locked(draft).event
            del self._step_claims[lease.request.stream_id]
            if self._step_claim_tokens.get(
                lease.token
            ) == lease.request.stream_id:
                self._step_claim_tokens.pop(lease.token, None)
            return event

    async def start_attempt(
        self,
        draft: EventDraft,
        *,
        lease_seconds: float = 30.0,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, DispatchAttemptStarted):
            raise TypeError(type(payload).__name__)
        if lease_seconds <= 0:
            raise ValueError("attempt lease duration must be positive")
        async with self._lock:
            key = (payload.command_id, payload.attempt_number)
            if key in self._attempts:
                return None
            existing_attempt = self._attempt_ids.get(payload.attempt_id)
            if existing_attempt is not None:
                existing_payload = existing_attempt.payload
                if (
                    existing_payload.command_id == payload.command_id
                    and existing_payload.attempt_number
                    == payload.attempt_number
                ):
                    return None
                raise ValueError(
                    f"duplicate attempt id: {payload.attempt_id}"
                )
            if payload.claim_token in self._attempt_claim_tokens:
                raise ValueError(
                    f"duplicate attempt claim token: {payload.claim_token}"
                )
            command = self._commands.get(payload.command_id)
            if command is None or command[0] != draft.stream_id:
                raise KeyError(payload.command_id)
            if payload.command_id in self._abandoned_commands:
                return None
            if payload.command_id in self._terminal_commands:
                return None
            expected_cause = self._dispatch_eligibility.get(payload.command_id)
            if draft.causation_id != expected_cause:
                return None
            if payload.attempt_number != self._attempt_count(payload.command_id) + 1:
                return None
            return self._append_locked(
                draft,
                attempt_lease_expires_at=self._clock() + lease_seconds,
            ).event

    async def renew_attempt(
        self,
        attempt_id: str,
        claim_token: str,
        *,
        lease_seconds: float,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("attempt lease duration must be positive")
        async with self._lock:
            current = self._attempt_leases.get(attempt_id)
            now = self._clock()
            if (
                current is None
                or current[0] != claim_token
                or current[1] <= now
            ):
                return False
            self._attempt_leases[attempt_id] = (
                claim_token,
                now + lease_seconds,
            )
            return True

    async def release_attempt(
        self,
        attempt_id: str,
        claim_token: str,
    ) -> None:
        async with self._lock:
            current = self._attempt_leases.get(attempt_id)
            if current is not None and current[0] == claim_token:
                self._attempt_leases.pop(attempt_id, None)

    async def start_reconcile(
        self,
        draft: EventDraft,
        *,
        lease_seconds: float = 30.0,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, CommandReconcileStarted):
            raise TypeError(type(payload).__name__)
        if lease_seconds <= 0:
            raise ValueError("reconcile lease duration must be positive")
        async with self._lock:
            key = (payload.attempt_id, payload.reconcile_number)
            if key in self._reconciles:
                return None
            if payload.reconcile_id in self._reconcile_ids:
                raise ValueError(
                    f"duplicate reconcile id: {payload.reconcile_id}"
                )
            attempt_event = self._attempt_ids.get(payload.attempt_id)
            if attempt_event is None:
                raise KeyError(payload.attempt_id)
            attempt_payload = attempt_event.payload
            if attempt_payload.command_id != payload.command_id:
                raise ValueError("reconcile attempt mismatch")
            if payload.command_id in self._terminal_commands:
                return None
            now = self._clock()
            attempt_lease = self._attempt_leases.get(payload.attempt_id)
            if attempt_lease is not None and attempt_lease[1] > now:
                return None
            reconcile_lease = self._reconcile_leases.get(payload.attempt_id)
            if reconcile_lease is not None and reconcile_lease[1] > now:
                return None
            receipt = self._external_operations.get(payload.attempt_id)
            expected_cause = (
                receipt.event_id
                if receipt is not None
                else attempt_event.event_id
            )
            if draft.causation_id != expected_cause:
                return None
            expected_number = 1 + sum(
                1
                for attempt_id, _ in self._reconciles
                if attempt_id == payload.attempt_id
            )
            if payload.reconcile_number != expected_number:
                return None
            return self._append_locked(
                draft,
                reconcile_lease_expires_at=now + lease_seconds,
            ).event

    async def renew_reconcile(
        self,
        reconcile_id: str,
        *,
        lease_seconds: float,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("reconcile lease duration must be positive")
        async with self._lock:
            event = self._reconcile_ids.get(reconcile_id)
            if event is None:
                raise KeyError(reconcile_id)
            attempt_id = event.payload.attempt_id
            current = self._reconcile_leases.get(attempt_id)
            now = self._clock()
            if (
                current is None
                or current[0] != reconcile_id
                or current[1] <= now
            ):
                return False
            self._reconcile_leases[attempt_id] = (
                reconcile_id,
                now + lease_seconds,
            )
            return True

    async def release_reconcile(self, reconcile_id: str) -> None:
        async with self._lock:
            event = self._reconcile_ids.get(reconcile_id)
            if event is None:
                return
            attempt_id = event.payload.attempt_id
            current = self._reconcile_leases.get(attempt_id)
            if current is not None and current[0] == reconcile_id:
                self._reconcile_leases.pop(attempt_id, None)

    async def confirm_no_effect(
        self,
        draft: EventDraft,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, DispatchAttemptConfirmedNoEffect):
            raise TypeError(type(payload).__name__)
        async with self._lock:
            existing = self._event_ids.get(draft.event_id)
            if existing is not None:
                if not self._same_event(existing, draft):
                    raise ValueError(f"event id conflict: {draft.event_id}")
                return existing
            reconcile_id = self._reconcile_event_ids.get(
                draft.causation_id
            )
            if reconcile_id is None:
                return None
            reconcile = self._reconcile_ids[reconcile_id].payload
            if (
                reconcile.command_id != payload.command_id
                or reconcile.attempt_id != payload.attempt_id
            ):
                return None
            current = self._reconcile_leases.get(payload.attempt_id)
            if (
                current is None
                or current[0] != reconcile_id
                or current[1] <= self._clock()
            ):
                return None
            attempt_event = self._attempt_ids[payload.attempt_id]
            attempt = attempt_event.payload
            if (
                payload.command_id in self._terminal_commands
                or attempt.attempt_number
                != self._attempt_count(payload.command_id)
                or payload.command_id in self._dispatch_eligibility
                or payload.attempt_id in self._external_operations
                or payload.attempt_id in self._attempt_terminal_events
            ):
                return None
            return self._append_locked(draft).event

    async def record_attempt_fact(
        self,
        draft: EventDraft,
    ) -> Event | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(
            payload,
            (ExternalOperationAccepted, CommandOutcomeReceived),
        ):
            raise TypeError(type(payload).__name__)
        if (
            isinstance(payload, CommandOutcomeReceived)
            and payload.attempt_id is None
        ):
            raise ValueError("attempt fact requires attempt identity")
        async with self._lock:
            existing = self._event_ids.get(draft.event_id)
            if existing is not None:
                if not self._same_event(existing, draft):
                    raise ValueError(f"event id conflict: {draft.event_id}")
                return existing
            attempt_id = payload.attempt_id
            if isinstance(payload, ExternalOperationAccepted):
                accepted = self._external_operations.get(attempt_id)
                if accepted is not None:
                    if accepted.payload != payload:
                        raise ValueError(
                            f"external operation conflict: {attempt_id}"
                        )
                    return accepted
            else:
                terminal = self._attempt_terminal_events.get(attempt_id)
                if terminal is not None:
                    if terminal.payload != payload:
                        raise ValueError(
                            f"attempt terminal conflict: {attempt_id}"
                        )
                    return terminal
            attempt_event = self._attempt_ids.get(attempt_id)
            if attempt_event is None:
                raise KeyError(attempt_id)
            attempt = attempt_event.payload
            if attempt.command_id != payload.command_id:
                raise ValueError("attempt fact command mismatch")
            command = self._command_definitions[payload.command_id]
            if (
                isinstance(payload, ExternalOperationAccepted)
                and not isinstance(command.effect, InvokeTool)
            ):
                raise ValueError("external receipt requires tool command")
            if draft.causation_id != attempt_event.event_id:
                reconcile_id = self._reconcile_event_ids.get(
                    draft.causation_id
                )
                if reconcile_id is None:
                    return None
                reconcile = self._reconcile_ids[reconcile_id].payload
                current = self._reconcile_leases.get(attempt_id)
                if (
                    reconcile.command_id != payload.command_id
                    or reconcile.attempt_id != attempt_id
                    or current is None
                    or current[0] != reconcile_id
                    or current[1] <= self._clock()
                ):
                    return None
            elif isinstance(command.effect, CancelTool):
                attempt_lease = self._attempt_leases.get(attempt_id)
                if (
                    attempt_lease is None
                    or attempt_lease[1] <= self._clock()
                ):
                    return None
            return self._append_locked(draft).event

    async def commit_pending_cancellation(
        self,
        target_draft: EventDraft,
        cancel_draft: EventDraft,
    ) -> tuple[Event, Event] | None:
        self._validate_pending_cancellation_drafts(
            target_draft,
            cancel_draft,
        )
        target_payload = target_draft.payload
        cancel_payload = cancel_draft.payload
        async with self._lock:
            for draft in (target_draft, cancel_draft):
                existing = self._event_ids.get(draft.event_id)
                if existing is not None and not self._same_event(
                    existing,
                    draft,
                ):
                    raise ValueError(f"event id conflict: {draft.event_id}")
            target_existing = self._event_ids.get(target_draft.event_id)
            cancel_existing = self._event_ids.get(cancel_draft.event_id)
            if target_existing is not None or cancel_existing is not None:
                if target_existing is None or cancel_existing is None:
                    raise RuntimeError("partial pending cancellation commit")
                return target_existing, cancel_existing

            attempt_id = cancel_payload.attempt_id
            attempt_event = self._attempt_ids.get(attempt_id)
            if attempt_event is None:
                raise KeyError(attempt_id)
            attempt = attempt_event.payload
            if attempt.command_id != cancel_payload.command_id:
                raise ValueError("cancel attempt command mismatch")
            cancel_command = self._command_definitions[
                cancel_payload.command_id
            ]
            if (
                not isinstance(cancel_command.effect, CancelTool)
                or cancel_command.effect.target_command_id
                != target_payload.command_id
            ):
                raise ValueError("cancel command target mismatch")
            if (
                target_draft.stream_id != cancel_draft.stream_id
                or self._commands[target_payload.command_id][0]
                != target_draft.stream_id
                or self._commands[cancel_payload.command_id][0]
                != cancel_draft.stream_id
            ):
                raise ValueError("pending cancellation stream mismatch")
            if (
                cancel_payload.command_id in self._terminal_commands
                or attempt_id in self._attempt_terminal_events
            ):
                return None

            now = self._clock()
            cause = cancel_draft.causation_id
            if cause == attempt_event.event_id:
                attempt_lease = self._attempt_leases.get(attempt_id)
                if attempt_lease is None or attempt_lease[1] <= now:
                    return None
            else:
                reconcile_id = self._reconcile_event_ids.get(cause)
                current = self._reconcile_leases.get(attempt_id)
                if reconcile_id is None or current is None:
                    return None
                reconcile = self._reconcile_ids[reconcile_id].payload
                if (
                    reconcile.command_id != cancel_payload.command_id
                    or reconcile.attempt_id != attempt_id
                    or current[0] != reconcile_id
                    or current[1] <= now
                ):
                    return None

            if (
                target_payload.command_id in self._terminal_commands
                or target_payload.command_id
                not in self._dispatch_eligibility
            ):
                return None

            target_event = self._append_locked(target_draft).event
            cancel_event = self._append_locked(cancel_draft).event
            return target_event, cancel_event

    async def ensure_recovery_required(
        self,
        draft: EventDraft,
    ) -> AppendResult | None:
        self._validate_internal_draft(draft)
        payload = draft.payload
        if not isinstance(payload, CommandRecoveryRequired):
            raise TypeError(type(payload).__name__)
        async with self._lock:
            key = (payload.command_id, payload.attempt_id)
            existing = self._recovery_required.get(key)
            if existing is not None:
                return AppendResult(existing, False)
            if payload.attempt_id not in self._attempt_ids:
                raise KeyError(payload.attempt_id)
            attempt_event = self._attempt_ids[payload.attempt_id]
            attempt = attempt_event.payload
            if attempt.command_id != payload.command_id:
                raise ValueError("recovery requirement command mismatch")
            if (
                payload.command_id in self._terminal_commands
                or payload.attempt_id in self._attempt_terminal_events
                or payload.attempt_id in self._external_operations
                or payload.command_id in self._dispatch_eligibility
                or attempt.attempt_number
                != self._attempt_count(payload.command_id)
            ):
                return None
            now = self._clock()
            attempt_lease = self._attempt_leases.get(payload.attempt_id)
            reconcile_lease = self._reconcile_leases.get(payload.attempt_id)
            reconcile_id = self._reconcile_event_ids.get(
                draft.causation_id
            )
            if reconcile_id is not None:
                if (
                    reconcile_lease is None
                    or reconcile_lease[0] != reconcile_id
                    or reconcile_lease[1] <= now
                ):
                    return None
            else:
                if draft.causation_id != attempt_event.event_id:
                    return None
                if (
                    attempt_lease is not None
                    and attempt_lease[1] > now
                ) or (
                    reconcile_lease is not None
                    and reconcile_lease[1] > now
                ):
                    return None
            return self._append_locked(draft)

    async def load_checkpoint(
        self,
        stream_id: str,
        journal_position: int,
        fingerprint: str,
    ) -> CanonicalState | None:
        async with self._lock:
            checkpoint = self._checkpoints.get(stream_id)
            if checkpoint is None:
                return None
            position, stored_fingerprint, state = checkpoint
            if position != journal_position or stored_fingerprint != fingerprint:
                return None
            return state

    async def save_checkpoint(
        self,
        state: CanonicalState,
        fingerprint: str,
    ) -> None:
        async with self._lock:
            self._checkpoints[state.stream_id] = (
                state.journal_position,
                fingerprint,
                state,
            )

    async def delete_checkpoint(self, stream_id: str) -> None:
        async with self._lock:
            self._checkpoints.pop(stream_id, None)

    def _append_locked(
        self,
        draft: EventDraft,
        *,
        attempt_lease_expires_at: float = 0.0,
        reconcile_lease_expires_at: float = 0.0,
    ) -> AppendResult:
        if draft.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported event schema version: {draft.schema_version}"
            )
        existing = self._event_ids.get(draft.event_id)
        if existing is not None:
            if not self._same_event(existing, draft):
                raise ValueError(f"event id conflict: {draft.event_id}")
            return AppendResult(existing, False)
        payload = draft.payload
        if (
            isinstance(payload, CommandOutcomeReceived)
            and payload.attempt_id is not None
        ):
            terminal = self._attempt_terminal_events.get(payload.attempt_id)
            if terminal is not None:
                if terminal.payload != payload:
                    raise ValueError(
                        f"attempt terminal conflict: {payload.attempt_id}"
                    )
                return AppendResult(terminal, False)
        stream_events = self._events.setdefault(draft.stream_id, [])
        event = Event(
            event_id=draft.event_id,
            stream_id=draft.stream_id,
            sequence=len(stream_events) + 1,
            payload=draft.payload,
            occurred_at=draft.occurred_at,
            causation_id=draft.causation_id,
            correlation_id=draft.correlation_id,
            schema_version=draft.schema_version,
            artifact_refs=draft.artifact_refs,
            delivery=draft.delivery,
        )
        self._prevalidate_index(event)
        stream_events.append(event)
        self._event_ids[event.event_id] = event
        self._index_event(
            event,
            attempt_lease_expires_at=attempt_lease_expires_at,
            reconcile_lease_expires_at=reconcile_lease_expires_at,
        )
        return AppendResult(event, True)

    def _index_event(
        self,
        event: Event,
        *,
        attempt_lease_expires_at: float = 0.0,
        reconcile_lease_expires_at: float = 0.0,
    ) -> None:
        payload = event.payload
        if isinstance(payload, StepCommitted):
            step = payload.step
            self._step_consumptions[step.trigger_event_id] = event.event_id
            self._step_ids.add(step.step_id)
            for command_id in step.decision.abandon_command_ids:
                self._abandoned_commands.add(command_id)
            for command_id, retry_attempt_id in step.retry_attempts:
                attempt_count = self._attempt_count(command_id)
                latest = self._attempts.get((command_id, attempt_count))
                latest_attempt_id = (
                    latest.payload.attempt_id
                    if latest is not None
                    else None
                )
                if (
                    command_id not in self._terminal_commands
                    and command_id not in self._dispatch_eligibility
                    and latest_attempt_id is not None
                    and latest_attempt_id == retry_attempt_id
                    and latest_attempt_id not in self._external_operations
                    and latest_attempt_id
                    not in self._attempt_terminal_events
                ):
                    self._dispatch_eligibility[command_id] = event.event_id
            for command in step.commands:
                self._commands[command.command_id] = (
                    event.stream_id,
                    event.event_id,
                )
                self._command_definitions[command.command_id] = command
                self._dispatch_eligibility[command.command_id] = event.event_id
        elif isinstance(payload, DispatchAttemptStarted):
            self._attempts[(
                payload.command_id,
                payload.attempt_number,
            )] = event
            self._attempt_ids[payload.attempt_id] = event
            self._attempt_claim_tokens[payload.claim_token] = event
            self._attempt_leases[payload.attempt_id] = (
                payload.claim_token,
                attempt_lease_expires_at,
            )
            self._dispatch_eligibility.pop(payload.command_id, None)
        elif isinstance(payload, CommandReconcileStarted):
            self._reconciles[(
                payload.attempt_id,
                payload.reconcile_number,
            )] = event
            self._reconcile_ids[payload.reconcile_id] = event
            self._reconcile_event_ids[event.event_id] = payload.reconcile_id
            self._reconcile_leases[payload.attempt_id] = (
                payload.reconcile_id,
                reconcile_lease_expires_at,
            )
        elif isinstance(payload, ExternalOperationAccepted):
            self._external_operations[payload.attempt_id] = event
            attempt = self._attempt_ids[payload.attempt_id].payload
            if (
                attempt.attempt_number
                == self._attempt_count(payload.command_id)
            ):
                self._dispatch_eligibility.pop(payload.command_id, None)
            self._clear_attempt_claims(payload.attempt_id)
        elif isinstance(payload, DispatchAttemptConfirmedNoEffect):
            attempt_event = self._attempt_ids[payload.attempt_id]
            attempt = attempt_event.payload
            if (
                payload.command_id not in self._terminal_commands
                and attempt.attempt_number
                == self._attempt_count(payload.command_id)
                and payload.command_id not in self._dispatch_eligibility
                and payload.attempt_id not in self._external_operations
            ):
                self._dispatch_eligibility[payload.command_id] = event.event_id
            self._clear_attempt_claims(payload.attempt_id)
        elif isinstance(payload, CommandRecoveryRequired):
            self._recovery_required[(
                payload.command_id,
                payload.attempt_id,
            )] = event
            self._clear_attempt_claims(payload.attempt_id)
        elif isinstance(payload, CommandOutcomeReceived):
            if payload.attempt_id is not None:
                self._attempt_terminal_events[payload.attempt_id] = event
                self._clear_attempt_claims(payload.attempt_id)
            self._terminal_commands.add(payload.command_id)
            self._dispatch_eligibility.pop(payload.command_id, None)

    def _clear_attempt_claims(self, attempt_id: str) -> None:
        self._attempt_leases.pop(attempt_id, None)
        self._reconcile_leases.pop(attempt_id, None)

    def _prevalidate_index(self, event: Event) -> None:
        payload = event.payload
        if isinstance(payload, StepCommitted):
            step = payload.step
            if step.step_id in self._step_ids:
                raise ValueError(f"duplicate step id: {step.step_id}")
            if step.trigger_event_id in self._step_consumptions:
                raise ValueError(
                    f"decision event consumed twice: {step.trigger_event_id}"
                )
            duplicate = next((
                command.command_id
                for command in step.commands
                if command.command_id in self._commands
            ), None)
            if duplicate is not None:
                raise ValueError(f"duplicate command id: {duplicate}")
        elif isinstance(payload, DispatchAttemptStarted):
            if (payload.command_id, payload.attempt_number) in self._attempts:
                raise ValueError("duplicate command attempt")
            if payload.attempt_id in self._attempt_ids:
                raise ValueError(f"duplicate attempt id: {payload.attempt_id}")
            if payload.claim_token in self._attempt_claim_tokens:
                raise ValueError(
                    f"duplicate attempt claim token: {payload.claim_token}"
                )
        elif isinstance(payload, CommandReconcileStarted):
            if (
                payload.attempt_id,
                payload.reconcile_number,
            ) in self._reconciles:
                raise ValueError("duplicate attempt reconcile")
            if payload.reconcile_id in self._reconcile_ids:
                raise ValueError(
                    f"duplicate reconcile id: {payload.reconcile_id}"
                )
        elif isinstance(payload, ExternalOperationAccepted):
            if payload.attempt_id in self._external_operations:
                raise ValueError(
                    f"external operation already accepted: "
                    f"{payload.attempt_id}"
                )
        elif isinstance(payload, CommandRecoveryRequired):
            if (
                payload.command_id,
                payload.attempt_id,
            ) in self._recovery_required:
                raise ValueError(
                    f"duplicate recovery requirement: {payload.command_id}"
                )
        elif isinstance(payload, CommandOutcomeReceived):
            if payload.command_id not in self._commands:
                raise KeyError(payload.command_id)
            if (
                payload.attempt_id is not None
                and payload.attempt_id in self._attempt_terminal_events
            ):
                raise ValueError(
                    f"attempt already terminal: {payload.attempt_id}"
                )

    def _validate_step_lease(
        self,
        lease: StepLease,
        draft: EventDraft,
    ) -> None:
        current = self._step_claims.get(lease.request.stream_id)
        if (
            not self._same_step_lease_identity(current, lease)
            or current.expires_at <= self._clock()
        ):
            raise LeaseLostError(lease.token)
        payload = draft.payload
        if not isinstance(payload, StepCommitted):
            raise TypeError(type(payload).__name__)
        step = payload.step
        request = lease.request
        if (
            draft.stream_id != request.stream_id
            or step.trigger_event_id != request.trigger_event_id
            or step.decision_cursor != request.decision_cursor
            or step.basis_state_version != request.basis_state_version
            or step.observed_journal_position
            != request.observed_journal_position
            or draft.causation_id != request.trigger_event_id
        ):
            raise ValueError("step does not match its claim")
        if request.trigger_event_id in self._step_consumptions:
            raise LeaseLostError(lease.token)

    @staticmethod
    def _same_step_lease_identity(
        left: StepLease | None,
        right: StepLease,
    ) -> bool:
        return (
            left is not None
            and left.request == right.request
            and left.token == right.token
            and left.owner_id == right.owner_id
            and left.generation == right.generation
        )

    def _attempt_count(self, command_id: str) -> int:
        return sum(1 for key in self._attempts if key[0] == command_id)

    @staticmethod
    def _same_delivery(event: Event, draft: EventDraft) -> bool:
        return (
            event.stream_id == draft.stream_id
            and event.payload == draft.payload
            and event.causation_id == draft.causation_id
            and event.correlation_id == draft.correlation_id
            and event.schema_version == draft.schema_version
            and event.artifact_refs == draft.artifact_refs
        )

    @staticmethod
    def _same_event(event: Event, draft: EventDraft) -> bool:
        return (
            MemoryJournal._same_delivery(event, draft)
            and event.delivery == draft.delivery
        )

    @staticmethod
    def _event_delivery_fingerprint(event: Event) -> str:
        return delivery_fingerprint(EventDraft(
            event_id=event.event_id,
            stream_id=event.stream_id,
            payload=event.payload,
            occurred_at=event.occurred_at,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
            schema_version=event.schema_version,
            artifact_refs=event.artifact_refs,
            delivery=event.delivery,
        ))

    @staticmethod
    def _validate_generic_append(draft: EventDraft) -> None:
        if draft.delivery is not None:
            raise ValueError("delivery events must use accept_delivery")
        if isinstance(
            draft.payload,
            (
                StepCommitted,
                DispatchAttemptStarted,
                CommandReconcileStarted,
                DispatchAttemptConfirmedNoEffect,
                CommandRecoveryRequired,
                UserMessageReceived,
                UserInterruptReceived,
            ),
        ):
            raise ValueError("conditional event requires its Journal method")
        if isinstance(draft.payload, ExternalOperationAccepted) or (
            isinstance(draft.payload, CommandOutcomeReceived)
            and draft.payload.attempt_id is not None
        ):
            raise ValueError("attempt fact requires conditional append")
        if isinstance(draft.payload, CommandOutcomeReceived):
            raise ValueError(
                "pending cancellation requires conditional commit"
            )

    @staticmethod
    def _validate_external_delivery(draft: EventDraft) -> None:
        if not isinstance(
            draft.payload,
            (UserMessageReceived, UserInterruptReceived),
        ):
            raise ValueError("delivery payload is not an external event")

    @staticmethod
    def _validate_pending_cancellation_drafts(
        target_draft: EventDraft,
        cancel_draft: EventDraft,
    ) -> None:
        target = target_draft.payload
        cancel = cancel_draft.payload
        if not isinstance(target, CommandOutcomeReceived) or (
            target.attempt_id is not None
        ):
            raise TypeError("target cancellation outcome is invalid")
        if not isinstance(cancel, CommandOutcomeReceived) or (
            cancel.attempt_id is None
        ):
            raise TypeError("cancel command outcome is invalid")
        if target.outcome.status is not OutcomeStatus.CANCELLED:
            raise ValueError("target outcome must be cancelled")
        if cancel.outcome.status is not OutcomeStatus.SUCCEEDED:
            raise ValueError("cancel command outcome must be succeeded")
        if (
            target_draft.event_id == cancel_draft.event_id
            or target_draft.delivery is not None
            or cancel_draft.delivery is not None
            or target_draft.causation_id != cancel_draft.causation_id
        ):
            raise ValueError("pending cancellation envelope is invalid")
        for draft in (target_draft, cancel_draft):
            if draft.schema_version != EVENT_SCHEMA_VERSION:
                raise ValueError(
                    "unsupported event schema version: "
                    f"{draft.schema_version}"
                )

    @staticmethod
    def _validate_internal_draft(draft: EventDraft) -> None:
        if draft.delivery is not None:
            raise ValueError("internal event cannot have delivery identity")
