from __future__ import annotations

from dataclasses import dataclass

from helperme.assistant.completion.judgment import CompletionGate, JudgmentPolicy
from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.toolsets import ToolSurface
from helperme.assistant.management import ManagementSurface
from helperme.llm.api import (
    InvalidLLMResponse,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)
from helperme.runtime import AgentRuntime, RuntimeStatus
from helperme.runtime.model import CanonicalState


@dataclass(frozen=True, slots=True)
class DriveResult:
    state: CanonicalState
    control_message: str | None = None


class SessionNotFoundError(LookupError):
    pass


MODEL_DECISION_ERRORS = (
    InvalidLLMResponse,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)


async def resume_session(
    runtime: AgentRuntime,
    surface: ToolSurface,
    session_id: str,
    management: ManagementSurface | None = None,
) -> CanonicalState:
    """Resume an explicitly selected Session; never select one for the caller."""

    if not await runtime.session_exists(session_id):
        raise SessionNotFoundError(session_id)
    events = await runtime.snapshot(session_id)
    await surface.rehydrate(session_id, events)
    if management is not None:
        await management.rehydrate(session_id, events)
    await runtime.recover_once(session_id)
    return await runtime.state(session_id)


async def drive_until_idle(
    runtime: AgentRuntime,
    session_id: str,
    *,
    policy: JudgmentPolicy | None = None,
    control: AssistantControlPlane | None = None,
) -> DriveResult:
    """Drive until waiting; only an explicit completion policy may finalize."""

    while True:
        step = await runtime.advance(session_id)
        control_result = None
        if step is not None and control is not None:
            control_result = await control.after_committed_step(
                session_id,
                step,
            )
        await runtime.dispatcher.wait_all()
        if control_result is not None:
            if policy is not None:
                await runtime.finalize(session_id)
            return DriveResult(
                await runtime.state(session_id),
                control_result.message,
            )
        if policy is not None:
            await policy.sync(runtime, session_id)
            if await policy.gate(runtime, session_id) is CompletionGate.PAUSE:
                return DriveResult(await runtime.state(session_id))
            await runtime.finalize(session_id)
        state = await runtime.state(session_id)
        if state.status in {
            RuntimeStatus.COMPLETED,
            RuntimeStatus.TERMINATED,
        }:
            return DriveResult(state)
        if state.status is RuntimeStatus.WAITING:
            if pending_authorization_ids(state):
                return DriveResult(state)
            if state.waiting_for == ("user_message",):
                return DriveResult(state)
        if state.status is RuntimeStatus.RUNNABLE or state.waiting_command_ids:
            continue
        return DriveResult(state)


def pending_authorization_ids(state: CanonicalState) -> tuple[str, ...]:
    return tuple(
        item.split(":", 1)[1]
        for item in state.waiting_for
        if item.startswith("authorization:")
    )
