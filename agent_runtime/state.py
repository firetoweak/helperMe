from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256

from agent_runtime.events import (
    CommandAuthorized,
    CommandRecoveryRequired,
    CommandReconcileStarted,
    CommandRejected,
    CommandOutcomeReceived,
    DispatchAttemptConfirmedNoEffect,
    DispatchAttemptStarted,
    Event,
    ExternalOperationAccepted,
    RuntimeCompleted,
    RuntimeTerminated,
    StepCommitted,
    TerminationRequested,
    UserInterruptReceived,
    UserMessageReceived,
)
from agent_runtime.model import (
    AttemptPhase,
    AttemptState,
    CancelTool,
    CanonicalState,
    CommandPhase,
    CommandState,
    DecisionState,
    RetrySemantics,
    RuntimeStatus,
    Step,
)


@dataclass(frozen=True, slots=True)
class DecisionFrame:
    trigger_event: Event
    state: DecisionState
    decision_cursor: int
    basis_state_version: str
    observed_journal_position: int


@dataclass(frozen=True, slots=True)
class RuntimeProjection:
    state: CanonicalState
    next_decision: DecisionFrame | None


class _StateBuilder:
    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        self.user_messages: list[str] = []
        self.interrupts: list[str | None] = []
        self.commands: dict[str, CommandState] = {}
        self.steps: list[Step] = []
        self.step_ids: set[str] = set()
        self.visible_event_ids: list[str] = []
        self.attempt_event_ids: dict[tuple[str, str], str] = {}
        self.dispatch_command_ids: dict[str, str] = {}
        self.reconcile_event_attempts: dict[str, tuple[str, str]] = {}
        self.reconcile_counts: dict[tuple[str, str], int] = {}
        self.recovery_required: set[tuple[str, str]] = set()
        self.canonical_outcome_event_ids: set[str] = set()
        self.terminal_event_id: str | None = None
        self.terminal_status: RuntimeStatus | None = None

    def version(self) -> str:
        content = json.dumps(
            [self.stream_id, *self.visible_event_ids],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(content).hexdigest()

    def decision_state(
        self,
        consumed_trigger_event_ids: tuple[str, ...],
    ) -> DecisionState:
        return DecisionState(
            stream_id=self.stream_id,
            version=self.version(),
            user_messages=tuple(self.user_messages),
            interrupts=tuple(self.interrupts),
            commands=tuple(self.commands.values()),
            prior_steps=tuple(self.steps),
            visible_event_ids=tuple(self.visible_event_ids),
            consumed_trigger_event_ids=consumed_trigger_event_ids,
        )

    def apply_regular(self, event: Event) -> None:
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            self.user_messages.append(payload.content)
        elif isinstance(payload, UserInterruptReceived):
            self.interrupts.append(payload.reason)
        elif isinstance(payload, CommandAuthorized):
            self._apply_authorized(event, payload)
        elif isinstance(payload, CommandRejected):
            self._apply_rejected(event, payload)
        elif isinstance(payload, DispatchAttemptStarted):
            self._apply_dispatch_started(event, payload)
        elif isinstance(payload, CommandReconcileStarted):
            self._apply_reconcile_started(event, payload)
        elif isinstance(payload, ExternalOperationAccepted):
            self._apply_external_accepted(event, payload)
        elif isinstance(payload, DispatchAttemptConfirmedNoEffect):
            self._apply_no_effect(event, payload)
        elif isinstance(payload, CommandRecoveryRequired):
            self._apply_recovery_required(event, payload)
        elif isinstance(payload, CommandOutcomeReceived):
            self._apply_outcome(event, payload)
        elif isinstance(payload, TerminationRequested):
            pass
        elif isinstance(payload, RuntimeCompleted):
            self._apply_runtime_completed(event, payload)
        elif isinstance(payload, RuntimeTerminated):
            self._apply_runtime_terminated(event, payload)
        else:
            raise TypeError(f"not a regular event: {type(payload).__name__}")
        self.visible_event_ids.append(event.event_id)

    def _apply_authorized(
        self,
        event: Event,
        payload: CommandAuthorized,
    ) -> None:
        state = self.commands[payload.command_id]
        if state.authorization_rejected_by_event_id is not None:
            raise ValueError(f"command already rejected: {payload.command_id}")
        if state.dispatch_eligible_by_event_id is not None:
            raise ValueError(
                f"command already authorized: {payload.command_id}"
            )
        if state.phase is not CommandPhase.PENDING:
            raise ValueError(f"command is not pending: {payload.command_id}")
        if event.causation_id != state.issued_by_event_id:
            raise ValueError(
                f"authorization causation mismatch: {payload.command_id}"
            )
        self.commands[payload.command_id] = replace(
            state,
            dispatch_eligible_by_event_id=event.event_id,
        )

    def _apply_rejected(
        self,
        event: Event,
        payload: CommandRejected,
    ) -> None:
        state = self.commands[payload.command_id]
        if state.authorization_rejected_by_event_id is not None:
            raise ValueError(f"command already rejected: {payload.command_id}")
        if state.dispatch_eligible_by_event_id is not None:
            raise ValueError(
                f"cannot reject authorized command: {payload.command_id}"
            )
        if state.phase is not CommandPhase.PENDING:
            raise ValueError(f"command is not pending: {payload.command_id}")
        if event.causation_id != state.issued_by_event_id:
            raise ValueError(
                f"rejection causation mismatch: {payload.command_id}"
            )
        self.commands[payload.command_id] = replace(
            state,
            authorization_rejected_by_event_id=event.event_id,
        )

    def _apply_dispatch_started(
        self,
        event: Event,
        payload: DispatchAttemptStarted,
    ) -> None:
        state = self.commands[payload.command_id]
        if state.phase is not CommandPhase.PENDING:
            raise ValueError(f"command is not pending: {payload.command_id}")
        if event.causation_id != state.dispatch_eligible_by_event_id:
            raise ValueError(
                f"dispatch causation mismatch: {payload.attempt_id}"
            )
        attempt_key = (payload.command_id, payload.attempt_id)
        if attempt_key in self.attempt_event_ids:
            raise ValueError(
                f"duplicate command attempt: {payload.attempt_id}"
            )
        if payload.attempt_number != len(state.attempts) + 1:
            raise ValueError(
                f"attempt number mismatch: {payload.attempt_id}"
            )
        attempt = AttemptState(
            attempt_id=payload.attempt_id,
            attempt_number=payload.attempt_number,
            started_event_id=event.event_id,
        )
        self.attempt_event_ids[attempt_key] = event.event_id
        self.dispatch_command_ids[event.event_id] = payload.command_id
        previous_attempts = tuple(
            replace(previous, superseded=True)
            if not previous.superseded
            else previous
            for previous in state.attempts
        )
        self.commands[payload.command_id] = replace(
            state,
            phase=CommandPhase.UNKNOWN,
            attempts=(*previous_attempts, attempt),
            dispatch_eligible_by_event_id=None,
        )

    def _apply_reconcile_started(
        self,
        event: Event,
        payload: CommandReconcileStarted,
    ) -> None:
        state = self.commands[payload.command_id]
        attempt = self._attempt(state, payload.attempt_id)
        expected_cause = (
            attempt.accepted_event_id
            if attempt.phase is AttemptPhase.RUNNING
            else attempt.started_event_id
        )
        if event.causation_id != expected_cause:
            raise ValueError(
                f"reconcile causation mismatch: {payload.reconcile_id}"
            )
        key = (payload.command_id, payload.attempt_id)
        expected_number = self.reconcile_counts.get(key, 0) + 1
        if payload.reconcile_number != expected_number:
            raise ValueError(
                f"reconcile number mismatch: {payload.reconcile_id}"
            )
        self.reconcile_counts[key] = expected_number
        self.reconcile_event_attempts[event.event_id] = key
        self.commands[payload.command_id] = replace(
            state,
            attempts=self._replace_attempt(
                state,
                replace(attempt, reconcile_count=expected_number),
            ),
        )

    def _apply_external_accepted(
        self,
        event: Event,
        payload: ExternalOperationAccepted,
    ) -> None:
        state = self.commands[payload.command_id]
        attempt = self._attempt(state, payload.attempt_id)
        self._validate_attempt_fact_cause(
            event,
            payload.command_id,
            attempt,
        )
        if (
            attempt.external_operation_id is not None
            and attempt.external_operation_id != payload.external_operation_id
        ):
            raise ValueError(
                f"external operation conflict: {payload.attempt_id}"
            )
        attempt_phase = (
            attempt.phase
            if attempt.phase is AttemptPhase.TERMINAL
            else AttemptPhase.RUNNING
        )
        revive_superseded = (
            attempt.superseded
            and state.outcome is None
            and state.phase is CommandPhase.PENDING
            and state.current_attempt is None
            and attempt_phase is AttemptPhase.RUNNING
        )
        updated = replace(
            attempt,
            phase=attempt_phase,
            external_operation_id=payload.external_operation_id,
            accepted_event_id=event.event_id,
            superseded=(
                False if revive_superseded else attempt.superseded
            ),
        )
        phase = state.phase
        dispatch_eligible_by_event_id = state.dispatch_eligible_by_event_id
        if (
            state.outcome is None
            and (not attempt.superseded or revive_superseded)
            and attempt_phase is AttemptPhase.RUNNING
        ):
            phase = CommandPhase.RUNNING
            dispatch_eligible_by_event_id = None
        self.commands[payload.command_id] = replace(
            state,
            phase=phase,
            attempts=self._replace_attempt(state, updated),
            dispatch_eligible_by_event_id=dispatch_eligible_by_event_id,
        )

    def _apply_no_effect(
        self,
        event: Event,
        payload: DispatchAttemptConfirmedNoEffect,
    ) -> None:
        state = self.commands[payload.command_id]
        attempt = self._attempt(state, payload.attempt_id)
        self._validate_reconcile_cause(event, payload.command_id, attempt)
        updated = replace(attempt, phase=AttemptPhase.NO_EFFECT)
        changes: dict[str, object] = {
            "attempts": self._replace_attempt(state, updated),
        }
        current_attempt = state.current_attempt
        if (
            state.outcome is None
            and state.phase is CommandPhase.UNKNOWN
            and current_attempt is not None
            and current_attempt.attempt_id == attempt.attempt_id
        ):
            changes.update(
                phase=CommandPhase.PENDING,
                dispatch_eligible_by_event_id=event.event_id,
            )
        self.commands[payload.command_id] = replace(state, **changes)

    def _apply_recovery_required(
        self,
        event: Event,
        payload: CommandRecoveryRequired,
    ) -> None:
        state = self.commands[payload.command_id]
        attempt = self._attempt(state, payload.attempt_id)
        self._validate_attempt_fact_cause(
            event,
            payload.command_id,
            attempt,
        )
        key = (payload.command_id, payload.attempt_id)
        if key in self.recovery_required:
            raise ValueError(
                f"duplicate recovery requirement: {payload.command_id}"
            )
        self.recovery_required.add(key)

    def _apply_outcome(
        self,
        event: Event,
        payload: CommandOutcomeReceived,
    ) -> None:
        state = self.commands[payload.command_id]
        attempts = state.attempts
        if payload.attempt_id is not None:
            attempt = self._attempt(state, payload.attempt_id)
            self._validate_attempt_fact_cause(
                event,
                payload.command_id,
                attempt,
            )
            if attempt.outcome is not None:
                raise ValueError(
                    f"attempt already terminal: {payload.attempt_id}"
                )
            attempts = self._replace_attempt(
                state,
                replace(
                    attempt,
                    phase=AttemptPhase.TERMINAL,
                    outcome=payload.outcome,
                ),
            )
        else:
            cancel_command_id = self.dispatch_command_ids.get(
                event.causation_id
            )
            if cancel_command_id is None:
                reconcile_source = self.reconcile_event_attempts.get(
                    event.causation_id
                )
                cancel_command_id = (
                    reconcile_source[0]
                    if reconcile_source is not None
                    else None
                )
            if cancel_command_id is None:
                raise ValueError(
                    f"outcome has no dispatch cause: {payload.command_id}"
                )
            cancel_effect = self.commands[
                cancel_command_id
            ].command.effect
            if (
                not isinstance(cancel_effect, CancelTool)
                or cancel_effect.target_command_id != payload.command_id
            ):
                raise ValueError(
                    f"outcome cancel cause mismatch: {payload.command_id}"
                )
        if state.outcome is None:
            self.canonical_outcome_event_ids.add(event.event_id)
            self.commands[payload.command_id] = replace(
                state,
                phase=CommandPhase.TERMINAL,
                attempts=attempts,
                outcome=payload.outcome,
                canonical_outcome_event_id=event.event_id,
                dispatch_eligible_by_event_id=None,
            )
        else:
            self.commands[payload.command_id] = replace(
                state,
                attempts=attempts,
            )

    def _validate_attempt_fact_cause(
        self,
        event: Event,
        command_id: str,
        attempt: AttemptState,
    ) -> None:
        if event.causation_id == attempt.started_event_id:
            return
        self._validate_reconcile_cause(event, command_id, attempt)

    def _validate_reconcile_cause(
        self,
        event: Event,
        command_id: str,
        attempt: AttemptState,
    ) -> None:
        if self.reconcile_event_attempts.get(event.causation_id) != (
            command_id,
            attempt.attempt_id,
        ):
            raise ValueError(f"attempt fact causation mismatch: {command_id}")

    @staticmethod
    def _attempt(state: CommandState, attempt_id: str) -> AttemptState:
        for attempt in state.attempts:
            if attempt.attempt_id == attempt_id:
                return attempt
        raise ValueError(
            f"unknown attempt for {state.command.command_id}: {attempt_id}"
        )

    @staticmethod
    def _replace_attempt(
        state: CommandState,
        updated: AttemptState,
    ) -> tuple[AttemptState, ...]:
        return tuple(
            updated if attempt.attempt_id == updated.attempt_id else attempt
            for attempt in state.attempts
        )

    def _apply_runtime_completed(
        self,
        event: Event,
        payload: RuntimeCompleted,
    ) -> None:
        if self.terminal_event_id is not None:
            raise ValueError("runtime already terminal")
        if payload.declared_by_event_id not in self.visible_event_ids:
            raise ValueError("completion declaration is missing")
        self.terminal_event_id = event.event_id
        self.terminal_status = RuntimeStatus.COMPLETED

    def _apply_runtime_terminated(
        self,
        event: Event,
        payload: RuntimeTerminated,
    ) -> None:
        if self.terminal_event_id is not None:
            raise ValueError("runtime already terminal")
        if payload.declared_by_event_id not in self.visible_event_ids:
            raise ValueError("termination declaration is missing")
        for command_id in payload.abandoned_command_ids:
            state = self.commands[command_id]
            self.commands[command_id] = replace(state, abandoned=True)
        self.terminal_event_id = event.event_id
        self.terminal_status = RuntimeStatus.TERMINATED

    def apply_step(
        self,
        event: Event,
        *,
        expected_basis_state_version: str | None = None,
    ) -> None:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            raise TypeError(f"not a step event: {type(payload).__name__}")
        if self.terminal_event_id is not None:
            raise ValueError("step committed after runtime terminal")
        step = payload.step
        if step.step_id in self.step_ids:
            raise ValueError(f"duplicate step id: {step.step_id}")
        if (
            expected_basis_state_version is not None
            and step.basis_state_version != expected_basis_state_version
        ):
            raise ValueError(
                f"step basis mismatch: {step.step_id}"
            )
        for command_id in step.decision.abandon_command_ids:
            state = self.commands[command_id]
            self.commands[command_id] = replace(state, abandoned=True)
        for command_id, retry_attempt_id in step.retry_attempts:
            state = self.commands[command_id]
            retry = state.command.recovery.retry_semantics
            if retry is RetrySemantics.PROHIBITED:
                raise ValueError(f"command retry is prohibited: {command_id}")
            if (
                retry is RetrySemantics.IDEMPOTENCY_KEY_REQUIRED
                and not state.command.idempotency_key
            ):
                raise ValueError(
                    f"command retry lacks idempotency key: {command_id}"
                )
            current = state.current_attempt
            if (
                state.phase is CommandPhase.UNKNOWN
                and current is not None
                and current.attempt_id == retry_attempt_id
            ):
                attempts = tuple(
                    replace(attempt, superseded=True)
                    if current is not None
                    and attempt.attempt_id == current.attempt_id
                    else attempt
                    for attempt in state.attempts
                )
                self.commands[command_id] = replace(
                    state,
                    phase=CommandPhase.PENDING,
                    attempts=attempts,
                    dispatch_eligible_by_event_id=event.event_id,
                )
        for command in step.commands:
            if command.command_id in self.commands:
                raise ValueError(f"duplicate command id: {command.command_id}")
            if isinstance(command.effect, CancelTool):
                if command.effect.target_command_id not in self.commands:
                    raise KeyError(command.effect.target_command_id)
            self.commands[command.command_id] = CommandState(
                command=command,
                phase=CommandPhase.PENDING,
                issued_by_event_id=event.event_id,
                dispatch_eligible_by_event_id=(
                    None
                    if command.requires_authorization
                    else event.event_id
                ),
            )
        self.steps.append(step)
        self.step_ids.add(step.step_id)
        self.visible_event_ids.append(event.event_id)


class StateProjector:
    def project(
        self,
        stream_id: str,
        events: tuple[Event, ...],
    ) -> RuntimeProjection:
        self._validate_stream(stream_id, events)
        step_events = self._index_step_events(events)
        event_sequences = {
            event.event_id: event.sequence for event in events
        }

        operational = _StateBuilder(stream_id)
        for event in events:
            if isinstance(event.payload, StepCommitted):
                operational.apply_step(event)
            else:
                operational.apply_regular(event)

        decision = _StateBuilder(stream_id)
        consumed: list[str] = []
        applied_step_event_ids: set[str] = set()
        next_trigger: Event | None = None

        for event in events:
            if isinstance(event.payload, StepCommitted):
                continue
            decision.apply_regular(event)
            if not self._requires_decision(event, decision):
                continue
            step_event = step_events.get(event.event_id)
            if step_event is None:
                next_trigger = event
                break
            step = step_event.payload.step
            expected_cursor = len(consumed) + 1
            if step.decision_cursor != expected_cursor:
                raise ValueError(
                    f"step decision cursor mismatch: {step.step_id}"
                )
            minimum_observed_position = max(
                event_sequences[event_id]
                for event_id in decision.visible_event_ids
            )
            if not (
                minimum_observed_position
                <= step.observed_journal_position
                < step_event.sequence
            ):
                raise ValueError(
                    f"step observed position mismatch: {step.step_id}"
                )
            decision.apply_step(
                step_event,
                expected_basis_state_version=decision.version(),
            )
            consumed.append(event.event_id)
            applied_step_event_ids.add(step_event.event_id)

        unapplied = {
            event.event_id
            for event in step_events.values()
        } - applied_step_event_ids
        if unapplied:
            raise ValueError(
                f"step commits cross an unconsumed decision event: "
                f"{sorted(unapplied)}"
            )

        decision_state = decision.decision_state(tuple(consumed))
        journal_position = events[-1].sequence if events else 0
        next_frame = (
            DecisionFrame(
                trigger_event=next_trigger,
                state=decision_state,
                decision_cursor=len(consumed) + 1,
                basis_state_version=decision_state.version,
                observed_journal_position=journal_position,
            )
            if next_trigger is not None
            else None
        )
        command_states = tuple(operational.commands.values())
        unauthorized_pending = tuple(
            state.command.command_id
            for state in command_states
            if state.phase is CommandPhase.PENDING
            and not state.abandoned
            and state.dispatch_eligible_by_event_id is None
            and state.authorization_rejected_by_event_id is None
        )
        waiting_command_ids = tuple(
            state.command.command_id
            for state in command_states
            if state.phase is not CommandPhase.TERMINAL
            and not state.abandoned
            and state.authorization_rejected_by_event_id is None
        )
        waiting_for = (
            ()
            if next_frame is not None
            else (
                tuple(
                    f"authorization:{command_id}"
                    for command_id in unauthorized_pending
                )
                + tuple(
                    f"command:{command_id}"
                    for command_id in waiting_command_ids
                    if command_id not in unauthorized_pending
                )
                or ("user_message",)
            )
        )
        if operational.terminal_status is not None:
            next_frame = None
            next_trigger = None
            waiting_for = ()
        state = CanonicalState(
            stream_id=stream_id,
            journal_position=journal_position,
            decision_cursor=len(consumed),
            status=(
                operational.terminal_status
                if operational.terminal_status is not None
                else (
                    RuntimeStatus.RUNNABLE
                    if next_frame is not None
                    else RuntimeStatus.WAITING
                )
            ),
            commands=command_states,
            steps=tuple(decision.steps),
            next_trigger_event_id=(
                next_trigger.event_id if next_trigger is not None else None
            ),
            waiting_command_ids=waiting_command_ids,
            waiting_for=waiting_for,
        )
        return RuntimeProjection(state=state, next_decision=next_frame)

    @staticmethod
    def _validate_stream(stream_id: str, events: tuple[Event, ...]) -> None:
        event_ids: set[str] = set()
        for expected_sequence, event in enumerate(events, start=1):
            if event.stream_id != stream_id:
                raise ValueError(
                    f"event belongs to another stream: {event.event_id}"
                )
            if event.sequence != expected_sequence:
                raise ValueError(
                    f"invalid event sequence: {event.event_id}"
                )
            if event.schema_version != 1:
                raise ValueError(
                    f"unsupported event schema version: "
                    f"{event.schema_version}"
                )
            if event.event_id in event_ids:
                raise ValueError(f"duplicate event id: {event.event_id}")
            event_ids.add(event.event_id)

    @staticmethod
    def _index_step_events(events: tuple[Event, ...]) -> dict[str, Event]:
        events_by_id = {event.event_id: event for event in events}
        result: dict[str, Event] = {}
        for event in events:
            payload = event.payload
            if not isinstance(payload, StepCommitted):
                continue
            trigger_id = payload.step.trigger_event_id
            trigger = events_by_id.get(trigger_id)
            if trigger is None:
                raise ValueError(
                    f"step trigger does not exist: {trigger_id}"
                )
            if trigger.sequence >= event.sequence:
                raise ValueError(
                    f"step precedes its trigger: {payload.step.step_id}"
                )
            if event.causation_id != trigger_id:
                raise ValueError(
                    f"step causation mismatch: {payload.step.step_id}"
                )
            if trigger_id in result:
                raise ValueError(
                    f"decision event consumed twice: {trigger_id}"
                )
            result[trigger_id] = event
        return result

    @staticmethod
    def _requires_decision(event: Event, state: _StateBuilder) -> bool:
        payload = event.payload
        if isinstance(payload, (UserMessageReceived, UserInterruptReceived)):
            return True
        if isinstance(payload, CommandRejected):
            return not state.commands[payload.command_id].abandoned
        if isinstance(payload, CommandRecoveryRequired):
            return not state.commands[payload.command_id].abandoned
        if isinstance(payload, CommandOutcomeReceived):
            command_state = state.commands[payload.command_id]
            return (
                event.event_id in state.canonical_outcome_event_ids
                and command_state.command.decision_on_outcome
                and not command_state.abandoned
            )
        return False
