from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256

from agent_runtime.events import (
    CommandOutcomeReceived,
    CommandRecoveryRequired,
    CommandReconcileStarted,
    DispatchAttemptConfirmedNoEffect,
    DispatchAttemptStarted,
    EventDraft,
    EventPayload,
    ExternalOperationAccepted,
    StepCommitted,
    UserInterruptReceived,
    UserMessageReceived,
)
from agent_runtime.model import (
    AttemptPhase,
    AttemptState,
    CancelTool,
    CanonicalState,
    Command,
    CommandOutcome,
    CommandPhase,
    CommandState,
    InvokeTool,
    ModelDecision,
    OutcomeStatus,
    RecoveryContract,
    RetrySemantics,
    RunningRecovery,
    RuntimeStatus,
    Step,
)


EVENT_SCHEMA_VERSION = 1
DELIVERY_FINGERPRINT_VERSION = 1
STATE_CODEC_VERSION = 1
STATE_PROJECTION_VERSION = "canonical-state-v1"

_USER_MESSAGE = "user.message.received"
_USER_INTERRUPT = "user.interrupt.received"
_STEP_COMMITTED = "step.committed"
_ATTEMPT_STARTED = "command.attempt.started"
_RECONCILE_STARTED = "command.reconcile.started"
_EXTERNAL_ACCEPTED = "command.external.accepted"
_NO_EFFECT = "command.attempt.no_effect"
_RECOVERY_REQUIRED = "command.recovery.required"
_OUTCOME_RECEIVED = "command.outcome.received"


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _require_object(
    value: object,
    keys: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"invalid {label} fields")
    return value


def _effect_to_data(effect: InvokeTool | CancelTool) -> dict[str, object]:
    if isinstance(effect, InvokeTool):
        return {
            "type": "invoke_tool",
            "name": effect.name,
            "arguments": [
                [key, _thaw(value)] for key, value in effect.arguments
            ],
        }
    if isinstance(effect, CancelTool):
        return {
            "type": "cancel_tool",
            "target_command_id": effect.target_command_id,
        }
    raise TypeError(type(effect).__name__)


def _effect_from_data(data: dict[str, object]) -> InvokeTool | CancelTool:
    kind = data["type"]
    if kind == "invoke_tool":
        _require_object(data, {"type", "name", "arguments"}, "invoke effect")
        return InvokeTool(
            name=data["name"],
            arguments=tuple(
                (item[0], item[1]) for item in data["arguments"]
            ),
        )
    if kind == "cancel_tool":
        _require_object(
            data,
            {"type", "target_command_id"},
            "cancel effect",
        )
        return CancelTool(data["target_command_id"])
    raise ValueError(f"unknown command effect: {kind}")


def _recovery_to_data(contract: RecoveryContract) -> dict[str, object]:
    return {
        "retry_semantics": contract.retry_semantics.value,
        "reconcile_unknown": contract.reconcile_unknown,
        "running_recovery": contract.running_recovery.value,
    }


def _recovery_from_data(data: dict[str, object]) -> RecoveryContract:
    _require_object(
        data,
        {"retry_semantics", "reconcile_unknown", "running_recovery"},
        "recovery contract",
    )
    return RecoveryContract(
        retry_semantics=RetrySemantics(data["retry_semantics"]),
        reconcile_unknown=data["reconcile_unknown"],
        running_recovery=RunningRecovery(data["running_recovery"]),
    )


def _outcome_to_data(outcome: CommandOutcome) -> dict[str, object]:
    return {
        "status": outcome.status.value,
        "value": _thaw(outcome.value),
        "error_type": outcome.error_type,
        "error_message": outcome.error_message,
    }


def _outcome_from_data(data: dict[str, object]) -> CommandOutcome:
    _require_object(
        data,
        {"status", "value", "error_type", "error_message"},
        "command outcome",
    )
    return CommandOutcome(
        status=OutcomeStatus(data["status"]),
        value=data["value"],
        error_type=data["error_type"],
        error_message=data["error_message"],
    )


def _command_to_data(command: Command) -> dict[str, object]:
    return {
        "command_id": command.command_id,
        "effect": _effect_to_data(command.effect),
        "recovery": _recovery_to_data(command.recovery),
        "idempotency_key": command.idempotency_key,
    }


def _command_from_data(data: dict[str, object]) -> Command:
    _require_object(
        data,
        {"command_id", "effect", "recovery", "idempotency_key"},
        "command",
    )
    return Command(
        command_id=data["command_id"],
        effect=_effect_from_data(data["effect"]),
        recovery=_recovery_from_data(data["recovery"]),
        idempotency_key=data["idempotency_key"],
    )


def _decision_to_data(decision: ModelDecision) -> dict[str, object]:
    return {
        "content": decision.content,
        "command_requests": [
            _effect_to_data(effect) for effect in decision.command_requests
        ],
        "abandon_command_ids": list(decision.abandon_command_ids),
        "retry_command_ids": list(decision.retry_command_ids),
    }


def _decision_from_data(data: dict[str, object]) -> ModelDecision:
    _require_object(
        data,
        {
            "content",
            "command_requests",
            "abandon_command_ids",
            "retry_command_ids",
        },
        "model decision",
    )
    return ModelDecision(
        content=data["content"],
        command_requests=tuple(
            _effect_from_data(effect)
            for effect in data["command_requests"]
        ),
        abandon_command_ids=tuple(data["abandon_command_ids"]),
        retry_command_ids=tuple(data["retry_command_ids"]),
    )


def _step_to_data(step: Step) -> dict[str, object]:
    return {
        "step_id": step.step_id,
        "trigger_event_id": step.trigger_event_id,
        "decision_cursor": step.decision_cursor,
        "basis_state_version": step.basis_state_version,
        "observed_journal_position": step.observed_journal_position,
        "decision": _decision_to_data(step.decision),
        "commands": [_command_to_data(command) for command in step.commands],
        "retry_attempts": [
            [command_id, attempt_id]
            for command_id, attempt_id in step.retry_attempts
        ],
    }


def _step_from_data(data: dict[str, object]) -> Step:
    _require_object(
        data,
        {
            "step_id",
            "trigger_event_id",
            "decision_cursor",
            "basis_state_version",
            "observed_journal_position",
            "decision",
            "commands",
            "retry_attempts",
        },
        "step",
    )
    return Step(
        step_id=data["step_id"],
        trigger_event_id=data["trigger_event_id"],
        decision_cursor=data["decision_cursor"],
        basis_state_version=data["basis_state_version"],
        observed_journal_position=data["observed_journal_position"],
        decision=_decision_from_data(data["decision"]),
        commands=tuple(
            _command_from_data(command) for command in data["commands"]
        ),
        retry_attempts=tuple(
            (command_id, attempt_id)
            for command_id, attempt_id in data["retry_attempts"]
        ),
    )


def encode_payload(payload: EventPayload) -> tuple[str, str]:
    if isinstance(payload, UserMessageReceived):
        kind = _USER_MESSAGE
        data = {"content": payload.content}
    elif isinstance(payload, UserInterruptReceived):
        kind = _USER_INTERRUPT
        data = {"reason": payload.reason}
    elif isinstance(payload, StepCommitted):
        kind = _STEP_COMMITTED
        data = {"step": _step_to_data(payload.step)}
    elif isinstance(payload, DispatchAttemptStarted):
        kind = _ATTEMPT_STARTED
        data = {
            "attempt_id": payload.attempt_id,
            "command_id": payload.command_id,
            "attempt_number": payload.attempt_number,
            "claim_token": payload.claim_token,
            "worker_id": payload.worker_id,
        }
    elif isinstance(payload, CommandReconcileStarted):
        kind = _RECONCILE_STARTED
        data = {
            "reconcile_id": payload.reconcile_id,
            "reconcile_number": payload.reconcile_number,
            "command_id": payload.command_id,
            "attempt_id": payload.attempt_id,
            "worker_id": payload.worker_id,
        }
    elif isinstance(payload, ExternalOperationAccepted):
        kind = _EXTERNAL_ACCEPTED
        data = {
            "command_id": payload.command_id,
            "attempt_id": payload.attempt_id,
            "external_operation_id": payload.external_operation_id,
        }
    elif isinstance(payload, DispatchAttemptConfirmedNoEffect):
        kind = _NO_EFFECT
        data = {
            "command_id": payload.command_id,
            "attempt_id": payload.attempt_id,
        }
    elif isinstance(payload, CommandRecoveryRequired):
        kind = _RECOVERY_REQUIRED
        data = {
            "command_id": payload.command_id,
            "attempt_id": payload.attempt_id,
            "reason": payload.reason,
            "allowed_actions": list(payload.allowed_actions),
        }
    elif isinstance(payload, CommandOutcomeReceived):
        kind = _OUTCOME_RECEIVED
        data = {
            "command_id": payload.command_id,
            "attempt_id": payload.attempt_id,
            "outcome": _outcome_to_data(payload.outcome),
        }
    else:
        raise TypeError(type(payload).__name__)
    return kind, _json_dumps(data)


def decode_payload(
    kind: str,
    schema_version: int,
    payload_json: str,
) -> EventPayload:
    if schema_version != EVENT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported event schema version: {schema_version}"
        )
    raw = json.loads(payload_json)
    if not isinstance(raw, dict):
        raise ValueError(f"invalid event payload: {kind}")
    data: dict[str, object] = raw
    if kind == _USER_MESSAGE:
        _require_object(data, {"content"}, kind)
        return UserMessageReceived(data["content"])
    if kind == _USER_INTERRUPT:
        _require_object(data, {"reason"}, kind)
        return UserInterruptReceived(data["reason"])
    if kind == _STEP_COMMITTED:
        _require_object(data, {"step"}, kind)
        return StepCommitted(_step_from_data(data["step"]))
    if kind == _ATTEMPT_STARTED:
        _require_object(
            data,
            {
                "attempt_id",
                "command_id",
                "attempt_number",
                "claim_token",
                "worker_id",
            },
            kind,
        )
        return DispatchAttemptStarted(**data)
    if kind == _RECONCILE_STARTED:
        _require_object(
            data,
            {
                "reconcile_id",
                "reconcile_number",
                "command_id",
                "attempt_id",
                "worker_id",
            },
            kind,
        )
        return CommandReconcileStarted(**data)
    if kind == _EXTERNAL_ACCEPTED:
        _require_object(
            data,
            {"command_id", "attempt_id", "external_operation_id"},
            kind,
        )
        return ExternalOperationAccepted(**data)
    if kind == _NO_EFFECT:
        _require_object(data, {"command_id", "attempt_id"}, kind)
        return DispatchAttemptConfirmedNoEffect(**data)
    if kind == _RECOVERY_REQUIRED:
        _require_object(
            data,
            {
                "command_id",
                "attempt_id",
                "reason",
                "allowed_actions",
            },
            kind,
        )
        return CommandRecoveryRequired(
            command_id=data["command_id"],
            attempt_id=data["attempt_id"],
            reason=data["reason"],
            allowed_actions=tuple(data["allowed_actions"]),
        )
    if kind == _OUTCOME_RECEIVED:
        _require_object(
            data,
            {"command_id", "attempt_id", "outcome"},
            kind,
        )
        return CommandOutcomeReceived(
            command_id=data["command_id"],
            attempt_id=data["attempt_id"],
            outcome=_outcome_from_data(data["outcome"]),
        )
    raise ValueError(f"unsupported event type: {kind}")


def delivery_fingerprint(draft: EventDraft) -> str:
    kind, payload_json = encode_payload(draft.payload)
    value = {
        "stream_id": draft.stream_id,
        "event_type": kind,
        "schema_version": draft.schema_version,
        "payload": json.loads(payload_json),
        "causation_id": draft.causation_id,
        "correlation_id": draft.correlation_id,
        "artifact_refs": list(draft.artifact_refs),
    }
    digest = sha256(_json_dumps(value).encode("utf-8")).hexdigest()
    return f"v{DELIVERY_FINGERPRINT_VERSION}:{digest}"


def _attempt_to_data(attempt: AttemptState) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id,
        "attempt_number": attempt.attempt_number,
        "started_event_id": attempt.started_event_id,
        "phase": attempt.phase.value,
        "external_operation_id": attempt.external_operation_id,
        "accepted_event_id": attempt.accepted_event_id,
        "outcome": (
            _outcome_to_data(attempt.outcome)
            if attempt.outcome is not None
            else None
        ),
        "superseded": attempt.superseded,
        "reconcile_count": attempt.reconcile_count,
    }


def _attempt_from_data(data: dict[str, object]) -> AttemptState:
    _require_object(
        data,
        {
            "attempt_id",
            "attempt_number",
            "started_event_id",
            "phase",
            "external_operation_id",
            "accepted_event_id",
            "outcome",
            "superseded",
            "reconcile_count",
        },
        "attempt state",
    )
    return AttemptState(
        attempt_id=data["attempt_id"],
        attempt_number=data["attempt_number"],
        started_event_id=data["started_event_id"],
        phase=AttemptPhase(data["phase"]),
        external_operation_id=data["external_operation_id"],
        accepted_event_id=data["accepted_event_id"],
        outcome=(
            _outcome_from_data(data["outcome"])
            if data["outcome"] is not None
            else None
        ),
        superseded=data["superseded"],
        reconcile_count=data["reconcile_count"],
    )


def _command_state_to_data(state: CommandState) -> dict[str, object]:
    return {
        "command": _command_to_data(state.command),
        "phase": state.phase.value,
        "issued_by_event_id": state.issued_by_event_id,
        "abandoned": state.abandoned,
        "attempts": [
            _attempt_to_data(attempt) for attempt in state.attempts
        ],
        "outcome": (
            _outcome_to_data(state.outcome)
            if state.outcome is not None
            else None
        ),
        "canonical_outcome_event_id": state.canonical_outcome_event_id,
        "dispatch_eligible_by_event_id": state.dispatch_eligible_by_event_id,
    }


def _command_state_from_data(data: dict[str, object]) -> CommandState:
    _require_object(
        data,
        {
            "command",
            "phase",
            "issued_by_event_id",
            "abandoned",
            "attempts",
            "outcome",
            "canonical_outcome_event_id",
            "dispatch_eligible_by_event_id",
        },
        "command state",
    )
    return CommandState(
        command=_command_from_data(data["command"]),
        phase=CommandPhase(data["phase"]),
        issued_by_event_id=data["issued_by_event_id"],
        abandoned=data["abandoned"],
        attempts=tuple(
            _attempt_from_data(attempt) for attempt in data["attempts"]
        ),
        outcome=(
            _outcome_from_data(data["outcome"])
            if data["outcome"] is not None
            else None
        ),
        canonical_outcome_event_id=data["canonical_outcome_event_id"],
        dispatch_eligible_by_event_id=data["dispatch_eligible_by_event_id"],
    )


def encode_state(state: CanonicalState) -> str:
    return _json_dumps({
        "codec_version": STATE_CODEC_VERSION,
        "projection_version": STATE_PROJECTION_VERSION,
        "state": {
            "stream_id": state.stream_id,
            "journal_position": state.journal_position,
            "decision_cursor": state.decision_cursor,
            "status": state.status.value,
            "commands": [
                _command_state_to_data(command) for command in state.commands
            ],
            "steps": [_step_to_data(step) for step in state.steps],
            "next_trigger_event_id": state.next_trigger_event_id,
            "waiting_command_ids": list(state.waiting_command_ids),
            "waiting_for": list(state.waiting_for),
        },
    })


def decode_state(state_json: str) -> CanonicalState:
    envelope = _require_object(
        json.loads(state_json),
        {"codec_version", "projection_version", "state"},
        "state checkpoint",
    )
    if envelope["codec_version"] != STATE_CODEC_VERSION:
        raise ValueError("unsupported state codec version")
    if envelope["projection_version"] != STATE_PROJECTION_VERSION:
        raise ValueError("unsupported state projection version")
    data = _require_object(
        envelope["state"],
        {
            "stream_id",
            "journal_position",
            "decision_cursor",
            "status",
            "commands",
            "steps",
            "next_trigger_event_id",
            "waiting_command_ids",
            "waiting_for",
        },
        "canonical state",
    )
    return CanonicalState(
        stream_id=data["stream_id"],
        journal_position=data["journal_position"],
        decision_cursor=data["decision_cursor"],
        status=RuntimeStatus(data["status"]),
        commands=tuple(
            _command_state_from_data(command) for command in data["commands"]
        ),
        steps=tuple(_step_from_data(step) for step in data["steps"]),
        next_trigger_event_id=data["next_trigger_event_id"],
        waiting_command_ids=tuple(data["waiting_command_ids"]),
        waiting_for=tuple(data["waiting_for"]),
    )
