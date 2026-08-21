from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias


Arguments: TypeAlias = tuple[tuple[str, object], ...]
MAX_JSON_VALUE_BYTES = 128 * 1024
MAX_JSON_VALUE_DEPTH = 32


def _require_str(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be str")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _require_optional_str(value: object, name: str) -> None:
    if value is not None:
        _require_str(value, name)


def freeze_value(value: object) -> object:
    budget = [MAX_JSON_VALUE_BYTES]
    return _freeze_value(value, 0, budget)


def _freeze_value(
    value: object,
    depth: int,
    budget: list[int],
) -> object:
    if depth > MAX_JSON_VALUE_DEPTH:
        raise ValueError("JSON value exceeds maximum depth")
    if value is None or type(value) in (bool, int, float, str):
        if type(value) is float and not isfinite(value):
            raise ValueError("JSON number must be finite")
        encoded = (
            value.encode("utf-8")
            if type(value) is str
            else str(value).encode("ascii")
        )
        budget[0] -= len(encoded) + 2
        if budget[0] < 0:
            raise ValueError("JSON value exceeds maximum size")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be str")
        budget[0] -= 2
        if budget[0] < 0:
            raise ValueError("JSON value exceeds maximum size")
        return MappingProxyType({
            key: _freeze_mapping_item(key, item, depth, budget)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        budget[0] -= 2
        if budget[0] < 0:
            raise ValueError("JSON value exceeds maximum size")
        return tuple(
            _freeze_value(item, depth + 1, budget)
            for item in value
        )
    raise TypeError(f"value is not JSON-compatible: {type(value).__name__}")


def _freeze_mapping_item(
    key: str,
    value: object,
    depth: int,
    budget: list[int],
) -> object:
    budget[0] -= len(key.encode("utf-8")) + 3
    if budget[0] < 0:
        raise ValueError("JSON value exceeds maximum size")
    return _freeze_value(value, depth + 1, budget)


@dataclass(frozen=True, slots=True)
class InvokeTool:
    name: str
    arguments: Arguments = ()

    def __post_init__(self) -> None:
        _require_str(self.name, "tool name")
        if type(self.arguments) is not tuple:
            raise TypeError("tool arguments must be tuple")
        if any(
            type(argument) is not tuple or len(argument) != 2
            for argument in self.arguments
        ):
            raise TypeError("tool arguments must contain key-value tuples")
        keys = [key for key, _ in self.arguments]
        if any(type(key) is not str for key in keys):
            raise TypeError("tool argument keys must be str")
        if len(keys) != len(set(keys)):
            raise ValueError("tool arguments contain duplicate keys")
        object.__setattr__(
            self,
            "arguments",
            tuple(
                (key, freeze_value(value))
                for key, value in self.arguments
            ),
        )

    def argument_dict(self) -> dict[str, object]:
        return dict(self.arguments)


@dataclass(frozen=True, slots=True)
class CancelTool:
    target_command_id: str

    def __post_init__(self) -> None:
        _require_str(self.target_command_id, "target command id")


CommandEffect: TypeAlias = InvokeTool | CancelTool


class RetrySemantics(str, Enum):
    SAFE = "safe"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    PROHIBITED = "prohibited"


class RunningRecovery(str, Enum):
    NONE = "none"
    QUERY = "query"
    CALLBACK = "callback"


@dataclass(frozen=True, slots=True)
class RecoveryContract:
    retry_semantics: RetrySemantics = RetrySemantics.PROHIBITED
    reconcile_unknown: bool = False
    running_recovery: RunningRecovery = RunningRecovery.NONE

    def __post_init__(self) -> None:
        if type(self.retry_semantics) is not RetrySemantics:
            raise TypeError("retry semantics is invalid")
        if type(self.reconcile_unknown) is not bool:
            raise TypeError("reconcile_unknown must be bool")
        if type(self.running_recovery) is not RunningRecovery:
            raise TypeError("running recovery is invalid")


@dataclass(frozen=True, slots=True)
class Command:
    """Committed side-effect.

    `requires_authorization` and `decision_on_outcome` are assembly
    information captured at issue time. They are not an approval policy
    and not inferred from the tool name.
    """

    command_id: str
    effect: CommandEffect
    recovery: RecoveryContract = RecoveryContract()
    idempotency_key: str | None = None
    requires_authorization: bool = False
    decision_on_outcome: bool | None = None

    def __post_init__(self) -> None:
        _require_str(self.command_id, "command id")
        if type(self.effect) not in (InvokeTool, CancelTool):
            raise TypeError("command effect is invalid")
        if type(self.recovery) is not RecoveryContract:
            raise TypeError("command recovery is invalid")
        _require_optional_str(self.idempotency_key, "idempotency key")
        if type(self.requires_authorization) is not bool:
            raise TypeError("requires_authorization must be bool")
        if (
            self.recovery.retry_semantics
            is RetrySemantics.IDEMPOTENCY_KEY_REQUIRED
            and not self.idempotency_key
        ):
            raise ValueError("idempotency key is required")
        if self.decision_on_outcome is None:
            object.__setattr__(
                self,
                "decision_on_outcome",
                isinstance(self.effect, InvokeTool),
            )
        elif type(self.decision_on_outcome) is not bool:
            raise TypeError("decision_on_outcome must be bool")


class LifecycleIntent(str, Enum):
    NONE = "none"
    COMPLETE = "complete"
    TERMINATE = "terminate"


@dataclass(frozen=True, slots=True)
class ModelDecision:
    content: str = ""
    command_requests: tuple[CommandEffect, ...] = ()
    abandon_command_ids: tuple[str, ...] = ()
    retry_command_ids: tuple[str, ...] = ()
    lifecycle_intent: LifecycleIntent = LifecycleIntent.NONE

    def __post_init__(self) -> None:
        if type(self.content) is not str:
            raise TypeError("decision content must be str")
        if type(self.command_requests) is not tuple:
            raise TypeError("command requests must be tuple")
        if any(
            type(request) not in (InvokeTool, CancelTool)
            for request in self.command_requests
        ):
            raise TypeError("command request is invalid")
        if type(self.abandon_command_ids) is not tuple:
            raise TypeError("abandon command ids must be tuple")
        if any(
            type(command_id) is not str or not command_id
            for command_id in self.abandon_command_ids
        ):
            raise ValueError("abandon command ids are invalid")
        if len(self.abandon_command_ids) != len(
            set(self.abandon_command_ids)
        ):
            raise ValueError("abandon command ids contain duplicates")
        if type(self.retry_command_ids) is not tuple:
            raise TypeError("retry command ids must be tuple")
        if any(
            type(command_id) is not str or not command_id
            for command_id in self.retry_command_ids
        ):
            raise ValueError("retry command ids are invalid")
        if len(self.retry_command_ids) != len(set(self.retry_command_ids)):
            raise ValueError("retry command ids contain duplicates")
        if set(self.abandon_command_ids) & set(self.retry_command_ids):
            raise ValueError("a command cannot be abandoned and retried")
        if type(self.lifecycle_intent) is not LifecycleIntent:
            raise TypeError("lifecycle intent must be LifecycleIntent")
        if (
            not self.content.strip()
            and not self.command_requests
            and not self.abandon_command_ids
            and not self.retry_command_ids
            and self.lifecycle_intent is LifecycleIntent.NONE
        ):
            raise ValueError("decision must contain content or effects")


@dataclass(frozen=True, slots=True)
class Step:
    step_id: str
    trigger_event_id: str
    decision_cursor: int
    basis_state_version: str
    observed_journal_position: int
    decision: ModelDecision
    commands: tuple[Command, ...]
    retry_attempts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_str(self.step_id, "step id")
        _require_str(self.trigger_event_id, "trigger event id")
        _require_str(self.basis_state_version, "basis state version")
        if (
            type(self.decision_cursor) is not int
            or self.decision_cursor < 1
        ):
            raise ValueError("decision cursor must be positive")
        if (
            type(self.observed_journal_position) is not int
            or self.observed_journal_position < 1
        ):
            raise ValueError("observed journal position must be positive")
        if type(self.decision) is not ModelDecision:
            raise TypeError("step decision is invalid")
        if type(self.commands) is not tuple:
            raise TypeError("step commands must be tuple")
        if any(type(command) is not Command for command in self.commands):
            raise TypeError("step command is invalid")
        if type(self.retry_attempts) is not tuple or any(
            type(item) is not tuple or len(item) != 2
            for item in self.retry_attempts
        ):
            raise TypeError("step retry attempts must be key-value tuples")
        command_ids = [command.command_id for command in self.commands]
        if len(command_ids) != len(set(command_ids)):
            raise ValueError("step commands contain duplicate ids")
        if tuple(
            command.effect for command in self.commands
        ) != self.decision.command_requests:
            raise ValueError("step commands do not match decision requests")
        retry_command_ids = tuple(
            command_id for command_id, _ in self.retry_attempts
        )
        retry_attempt_ids = tuple(
            attempt_id for _, attempt_id in self.retry_attempts
        )
        if any(
            type(command_id) is not str or not command_id
            for command_id in retry_command_ids
        ):
            raise ValueError("step retry command ids are invalid")
        if retry_command_ids != self.decision.retry_command_ids:
            raise ValueError("step retry attempts do not match decision")
        if (
            any(
                type(attempt_id) is not str or not attempt_id
                for attempt_id in retry_attempt_ids
            )
            or len(retry_attempt_ids) != len(set(retry_attempt_ids))
        ):
            raise ValueError("step retry attempt ids are invalid")


class OutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    status: OutcomeStatus
    value: object | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not OutcomeStatus:
            raise TypeError("outcome status is invalid")
        _require_optional_str(self.error_type, "outcome error type")
        _require_optional_str(self.error_message, "outcome error message")
        object.__setattr__(self, "value", freeze_value(self.value))


class CommandPhase(str, Enum):
    """Execution lifecycle only.

    Authorization is an orthogonal dispatch gate. PENDING means the
    command is issued and not yet terminal; it does not mean Dispatcher
    may claim it.
    """

    PENDING = "pending"
    UNKNOWN = "unknown"
    RUNNING = "running"
    TERMINAL = "terminal"


class AttemptPhase(str, Enum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    NO_EFFECT = "no_effect"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class AttemptState:
    attempt_id: str
    attempt_number: int
    started_event_id: str
    phase: AttemptPhase = AttemptPhase.UNKNOWN
    external_operation_id: str | None = None
    accepted_event_id: str | None = None
    outcome: CommandOutcome | None = None
    superseded: bool = False
    reconcile_count: int = 0


@dataclass(frozen=True, slots=True)
class CommandState:
    command: Command
    phase: CommandPhase
    issued_by_event_id: str
    abandoned: bool = False
    attempts: tuple[AttemptState, ...] = ()
    outcome: CommandOutcome | None = None
    canonical_outcome_event_id: str | None = None
    dispatch_eligible_by_event_id: str | None = None
    authorization_rejected_by_event_id: str | None = None

    @property
    def attempt_ids(self) -> tuple[str, ...]:
        return tuple(attempt.attempt_id for attempt in self.attempts)

    @property
    def current_attempt(self) -> AttemptState | None:
        for attempt in reversed(self.attempts):
            if not attempt.superseded:
                return attempt
        return None


@dataclass(frozen=True, slots=True)
class DecisionState:
    stream_id: str
    version: str
    user_messages: tuple[str, ...]
    interrupts: tuple[str | None, ...]
    commands: tuple[CommandState, ...]
    prior_steps: tuple[Step, ...]
    visible_event_ids: tuple[str, ...]
    consumed_trigger_event_ids: tuple[str, ...]

    def command(self, command_id: str) -> CommandState:
        for state in self.commands:
            if state.command.command_id == command_id:
                return state
        raise KeyError(command_id)


class RuntimeStatus(str, Enum):
    RUNNABLE = "runnable"
    WAITING = "waiting"
    COMPLETED = "completed"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class CanonicalState:
    stream_id: str
    journal_position: int
    decision_cursor: int
    status: RuntimeStatus
    commands: tuple[CommandState, ...]
    steps: tuple[Step, ...]
    next_trigger_event_id: str | None
    waiting_command_ids: tuple[str, ...]
    waiting_for: tuple[str, ...]

    def command(self, command_id: str) -> CommandState:
        for state in self.commands:
            if state.command.command_id == command_id:
                return state
        raise KeyError(command_id)
