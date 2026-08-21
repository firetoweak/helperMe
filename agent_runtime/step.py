from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from agent_runtime.events import Event, EventDraft, StepCommitted
from agent_runtime.journal import Journal, StepLease
from agent_runtime.model import (
    CancelTool,
    Command,
    CommandPhase,
    InvokeTool,
    ModelDecision,
    RecoveryContract,
    RetrySemantics,
    Step,
)
from agent_runtime.state import DecisionFrame, StateProjector


IdFactory = Callable[[str], str]


def random_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class DecisionMaker(Protocol):
    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        ...


class StepRunner:
    def __init__(
        self,
        journal: Journal,
        projector: StateProjector,
        decision_maker: DecisionMaker,
        recovery_contracts: Mapping[str, RecoveryContract],
        id_factory: IdFactory = random_id,
        requires_authorization: Mapping[str, bool] | None = None,
    ) -> None:
        self._journal = journal
        self._projector = projector
        self._decision_maker = decision_maker
        self._recovery_contracts = dict(recovery_contracts)
        self._requires_authorization = dict(requires_authorization or {})
        self._id_factory = id_factory

    async def commit(
        self,
        frame: DecisionFrame,
        lease: StepLease,
    ) -> Event:
        decision = await self._decision_maker.decide(frame)
        if type(decision) is not ModelDecision:
            raise TypeError("decision maker returned an invalid decision")
        known_commands = {
            state.command.command_id: state
            for state in frame.state.commands
        }
        for command_id in decision.abandon_command_ids:
            if command_id not in known_commands:
                raise KeyError(command_id)
        for command_id in decision.retry_command_ids:
            state = known_commands[command_id]
            if state.phase is not CommandPhase.UNKNOWN:
                raise ValueError(f"command is not unknown: {command_id}")
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
        commands: list[Command] = []
        for request in decision.command_requests:
            command_id = self._id_factory("command")
            if isinstance(request, InvokeTool):
                command = Command(
                    command_id=command_id,
                    effect=request,
                    recovery=self._recovery_contracts[request.name],
                    idempotency_key=command_id,
                    requires_authorization=self._requires_authorization.get(
                        request.name,
                        False,
                    ),
                )
            elif isinstance(request, CancelTool):
                if request.target_command_id not in known_commands:
                    raise KeyError(request.target_command_id)
                command = Command(
                    command_id=command_id,
                    effect=request,
                )
            else:
                raise TypeError(type(request).__name__)
            commands.append(command)
        command_tuple = tuple(commands)
        retry_attempts = tuple(
            (
                command_id,
                known_commands[command_id].current_attempt.attempt_id,
            )
            for command_id in decision.retry_command_ids
        )
        step = Step(
            step_id=self._id_factory("step"),
            trigger_event_id=frame.trigger_event.event_id,
            decision_cursor=frame.decision_cursor,
            basis_state_version=frame.basis_state_version,
            observed_journal_position=frame.observed_journal_position,
            decision=decision,
            commands=command_tuple,
            retry_attempts=retry_attempts,
        )
        return await self._journal.commit_step(lease, EventDraft(
            event_id=self._id_factory("event"),
            stream_id=frame.trigger_event.stream_id,
            payload=StepCommitted(step),
            occurred_at=datetime.now(timezone.utc),
            causation_id=frame.trigger_event.event_id,
        ))
