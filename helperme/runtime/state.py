from __future__ import annotations

import json
from dataclasses import dataclass, replace
from hashlib import sha256

from helperme.runtime.codec import EVENT_SCHEMA_VERSION
from helperme.runtime.events import (
    CommandAuthorized,
    CommandRejected,
    CommandOutcomeReceived,
    DomainFactCommitted,
    DispatchAttemptStarted,
    Event,
    RuntimeCompleted,
    RuntimeTerminated,
    StepCommitted,
    TerminationRequested,
    UserMessageReceived,
)
from helperme.runtime.model import (
    AttemptPhase,
    AttemptState,
    CanonicalState,
    CommandPhase,
    CommandState,
    DecisionState,
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
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.user_messages: list[str] = []
        self.commands: dict[str, CommandState] = {}
        self.steps: list[Step] = []
        self.step_ids: set[str] = set()
        self.visible_event_ids: list[str] = []
        self.attempt_event_ids: dict[tuple[str, str], str] = {}
        self.dispatch_command_ids: dict[str, str] = {}
        self.canonical_outcome_event_ids: set[str] = set()
        self.terminal_event_id: str | None = None
        self.terminal_status: RuntimeStatus | None = None

    def version(self) -> str:
        content = json.dumps(
            [self.session_id, *self.visible_event_ids],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(content).hexdigest()

    def decision_state(
        self,
        consumed_trigger_event_ids: tuple[str, ...],
    ) -> DecisionState:
        return DecisionState(
            session_id=self.session_id,
            version=self.version(),
            user_messages=tuple(self.user_messages),
            commands=tuple(self.commands.values()),
            prior_steps=tuple(self.steps),
            visible_event_ids=tuple(self.visible_event_ids),
            consumed_trigger_event_ids=consumed_trigger_event_ids,
        )

    def apply_regular(self, event: Event) -> None:
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            self.user_messages.append(payload.content)
        elif isinstance(payload, CommandAuthorized):
            self._apply_authorized(event, payload)
        elif isinstance(payload, CommandRejected):
            self._apply_rejected(event, payload)
        elif isinstance(payload, DispatchAttemptStarted):
            self._apply_dispatch_started(event, payload)
        elif isinstance(payload, CommandOutcomeReceived):
            self._apply_outcome(event, payload)
        elif isinstance(payload, TerminationRequested):
            pass
        elif isinstance(payload, RuntimeCompleted):
            self._apply_runtime_completed(event, payload)
        elif isinstance(payload, RuntimeTerminated):
            self._apply_runtime_terminated(event, payload)
        elif isinstance(payload, DomainFactCommitted):
            pass
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
            raise ValueError(f"command already authorized: {payload.command_id}")
        if state.phase is not CommandPhase.PENDING:
            raise ValueError(f"command is not pending: {payload.command_id}")
        if event.causation_id != state.issued_by_event_id:
            raise ValueError(f"authorization causation mismatch: {payload.command_id}")
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
            raise ValueError(f"cannot reject authorized command: {payload.command_id}")
        if state.phase is not CommandPhase.PENDING:
            raise ValueError(f"command is not pending: {payload.command_id}")
        if event.causation_id != state.issued_by_event_id:
            raise ValueError(f"rejection causation mismatch: {payload.command_id}")
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
            raise ValueError(f"dispatch causation mismatch: {payload.attempt_id}")
        attempt_key = (payload.command_id, payload.attempt_id)
        if attempt_key in self.attempt_event_ids:
            raise ValueError(f"duplicate command attempt: {payload.attempt_id}")
        if payload.attempt_number != len(state.attempts) + 1:
            raise ValueError(f"attempt number mismatch: {payload.attempt_id}")
        attempt = AttemptState(
            attempt_id=payload.attempt_id,
            attempt_number=payload.attempt_number,
            started_event_id=event.event_id,
        )
        self.attempt_event_ids[attempt_key] = event.event_id
        self.dispatch_command_ids[event.event_id] = payload.command_id
        self.commands[payload.command_id] = replace(
            state,
            phase=CommandPhase.UNKNOWN,
            attempts=(*state.attempts, attempt),
            dispatch_eligible_by_event_id=None,
        )

    def _apply_outcome(
        self,
        event: Event,
        payload: CommandOutcomeReceived,
    ) -> None:
        state = self.commands[payload.command_id]
        attempts = state.attempts
        if payload.attempt_id is None:
            raise ValueError(f"outcome has no attempt: {payload.command_id}")
        attempt = self._attempt(state, payload.attempt_id)
        if event.causation_id != attempt.started_event_id:
            raise ValueError(f"attempt fact causation mismatch: {payload.command_id}")
        if attempt.outcome is not None:
            raise ValueError(f"attempt already terminal: {payload.attempt_id}")
        attempts = self._replace_attempt(
            state,
            replace(
                attempt,
                phase=AttemptPhase.TERMINAL,
                outcome=payload.outcome,
            ),
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
            raise ValueError(f"step basis mismatch: {step.step_id}")
        for command in step.commands:
            if command.command_id in self.commands:
                raise ValueError(f"duplicate command id: {command.command_id}")
            self.commands[command.command_id] = CommandState(
                command=command,
                phase=CommandPhase.PENDING,
                issued_by_event_id=event.event_id,
                dispatch_eligible_by_event_id=(
                    None if command.requires_authorization else event.event_id
                ),
            )
        self.steps.append(step)
        self.step_ids.add(step.step_id)
        self.visible_event_ids.append(event.event_id)


class StateProjector:
    def project(
        self,
        session_id: str,
        events: tuple[Event, ...],
    ) -> RuntimeProjection:
        self._validate_session(session_id, events)
        step_events = self._index_step_events(events)
        event_sequences = {event.event_id: event.sequence for event in events}
        issuing_observed = {
            event.event_id: event.payload.step.observed_journal_position
            for event in events
            if isinstance(event.payload, StepCommitted)
        }

        operational = _StateBuilder(session_id)
        for event in events:
            if isinstance(event.payload, StepCommitted):
                operational.apply_step(event)
            else:
                operational.apply_regular(event)

        decision = _StateBuilder(session_id)
        consumed: list[str] = []
        applied_step_event_ids: set[str] = set()
        next_trigger: Event | None = None

        for event in events:
            if isinstance(event.payload, StepCommitted):
                continue
            if event.event_id in decision.visible_event_ids:
                continue
            decision.apply_regular(event)
            step_event = step_events.get(event.event_id)
            if step_event is not None:
                step = step_event.payload.step
                if isinstance(event.payload, UserMessageReceived):
                    self._include_waited_follow_up_events(
                        decision,
                        events,
                        event,
                        operational,
                        event_sequences,
                        issuing_observed,
                        until_sequence=step.observed_journal_position,
                    )
                expected_cursor = len(consumed) + 1
                if step.decision_cursor != expected_cursor:
                    raise ValueError(f"step decision cursor mismatch: {step.step_id}")
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
                continue
            if not self._requires_decision(
                event,
                decision,
                events,
                issuing_observed,
            ):
                continue
            if isinstance(event.payload, UserMessageReceived) and (
                self._user_message_blocked_by_in_flight(
                    event,
                    operational,
                    issuing_observed,
                )
            ):
                break
            next_trigger = event
            if isinstance(event.payload, UserMessageReceived):
                self._include_waited_follow_up_events(
                    decision,
                    events,
                    event,
                    operational,
                    event_sequences,
                    issuing_observed,
                    until_sequence=events[-1].sequence,
                )
            break

        unapplied = {
            event.event_id for event in step_events.values()
        } - applied_step_event_ids
        if unapplied:
            raise ValueError(
                f"step commits cross an unconsumed decision event: {sorted(unapplied)}"
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
                    f"authorization:{command_id}" for command_id in unauthorized_pending
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
            session_id=session_id,
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
    def _validate_session(session_id: str, events: tuple[Event, ...]) -> None:
        event_ids: set[str] = set()
        for expected_sequence, event in enumerate(events, start=1):
            if event.session_id != session_id:
                raise ValueError(f"event belongs to another session: {event.event_id}")
            if event.sequence != expected_sequence:
                raise ValueError(f"invalid event sequence: {event.event_id}")
            if event.schema_version != EVENT_SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported event schema version: {event.schema_version}"
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
                raise ValueError(f"step trigger does not exist: {trigger_id}")
            if trigger.sequence >= event.sequence:
                raise ValueError(f"step precedes its trigger: {payload.step.step_id}")
            if event.causation_id != trigger_id:
                raise ValueError(f"step causation mismatch: {payload.step.step_id}")
            if trigger_id in result:
                raise ValueError(f"decision event consumed twice: {trigger_id}")
            result[trigger_id] = event
        return result

    @staticmethod
    def _requires_decision(
        event: Event,
        state: _StateBuilder,
        events: tuple[Event, ...],
        issuing_observed: dict[str, int],
    ) -> bool:
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            return True
        if isinstance(payload, DomainFactCommitted):
            return payload.requests_decision
        if isinstance(payload, CommandRejected):
            return not state.commands[payload.command_id].abandoned
        if isinstance(payload, CommandOutcomeReceived):
            command_state = state.commands[payload.command_id]
            if not (
                event.event_id in state.canonical_outcome_event_ids
                and command_state.command.decision_on_outcome
                and not command_state.abandoned
                and _parallel_decision_group_closed(state, command_state)
            ):
                return False
            observed = issuing_observed[command_state.issued_by_event_id]
            if any(
                isinstance(item.payload, UserMessageReceived)
                and item.sequence > observed
                for item in events
            ):
                return False
            return True
        return False

    @staticmethod
    def _user_message_blocked_by_in_flight(
        message: Event,
        operational: _StateBuilder,
        issuing_observed: dict[str, int],
    ) -> bool:
        for command_state in operational.commands.values():
            if command_state.abandoned:
                continue
            if (
                issuing_observed[command_state.issued_by_event_id]
                >= message.sequence
            ):
                continue
            if not command_state.attempts:
                continue
            if command_state.phase is CommandPhase.TERMINAL:
                continue
            return True
        return False

    @staticmethod
    def _include_waited_follow_up_events(
        decision: _StateBuilder,
        events: tuple[Event, ...],
        trigger: Event,
        operational: _StateBuilder,
        event_sequences: dict[str, int],
        issuing_observed: dict[str, int],
        until_sequence: int,
    ) -> None:
        past_trigger = False
        for event in events:
            if event.event_id == trigger.event_id:
                past_trigger = True
                continue
            if not past_trigger:
                continue
            if event.sequence > until_sequence:
                return
            if event.event_id in decision.visible_event_ids:
                continue
            if not _is_waited_follow_up(
                event,
                trigger.sequence,
                operational,
                issuing_observed,
            ):
                continue
            decision.apply_regular(event)
            if _waited_batch_complete(
                decision,
                trigger.sequence,
                operational,
                issuing_observed,
            ):
                return


def _is_waited_follow_up(
    event: Event,
    trigger_sequence: int,
    operational: _StateBuilder,
    issuing_observed: dict[str, int],
) -> bool:
    payload = event.payload
    if isinstance(payload, (DispatchAttemptStarted, CommandOutcomeReceived)):
        command_state = operational.commands[payload.command_id]
        return (
            issuing_observed[command_state.issued_by_event_id] < trigger_sequence
        )
    return False


def _waited_batch_complete(
    decision: _StateBuilder,
    trigger_sequence: int,
    operational: _StateBuilder,
    issuing_observed: dict[str, int],
) -> bool:
    for command_state in operational.commands.values():
        if command_state.abandoned:
            continue
        if issuing_observed[command_state.issued_by_event_id] >= trigger_sequence:
            continue
        if not command_state.attempts:
            continue
        frozen = decision.commands.get(command_state.command.command_id)
        if frozen is None or frozen.phase is not CommandPhase.TERMINAL:
            return False
    return True


def _parallel_decision_group_closed(
    state: _StateBuilder,
    command_state: CommandState,
) -> bool:
    """Return whether an unordered parallel command group is terminal.

    Commands issued by one Step with ``decision_on_outcome=True`` form a
    set.  Their start and completion order is journal evidence only; it
    must not create intermediate model decisions.
    """
    issued_by = command_state.issued_by_event_id
    for sibling in state.commands.values():
        if sibling.issued_by_event_id != issued_by:
            continue
        if not sibling.command.decision_on_outcome:
            continue
        if sibling.abandoned:
            continue
        if sibling.phase is not CommandPhase.TERMINAL:
            return False
    return True
