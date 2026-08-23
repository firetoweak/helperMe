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


class StreamNotFoundError(LookupError):
    pass


MODEL_DECISION_ERRORS = (
    InvalidLLMResponse,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)


async def resume_stream(
    runtime: AgentRuntime,
    surface: ToolSurface,
    stream_id: str,
    management: ManagementSurface | None = None,
) -> CanonicalState:
    """Resume an explicitly selected Stream; never select one for the caller."""

    if not await runtime.stream_exists(stream_id):
        raise StreamNotFoundError(stream_id)
    events = await runtime.snapshot(stream_id)
    await surface.rehydrate(stream_id, events)
    if management is not None:
        await management.rehydrate(stream_id, events)
    await runtime.recover_once(stream_id)
    return await runtime.state(stream_id)


async def drive_until_idle(
    runtime: AgentRuntime,
    stream_id: str,
    *,
    policy: JudgmentPolicy | None = None,
    control: AssistantControlPlane | None = None,
) -> DriveResult:
    while True:
        step = await runtime.advance(stream_id)
        control_result = None
        if step is not None and control is not None:
            control_result = await control.after_committed_step(
                stream_id,
                step,
            )
        await runtime.dispatcher.wait_all()
        if control_result is not None:
            await runtime.finalize(stream_id)
            return DriveResult(
                await runtime.state(stream_id),
                control_result.message,
            )
        if policy is not None:
            await policy.sync(runtime, stream_id)
            if await policy.gate(runtime, stream_id) is CompletionGate.PAUSE:
                return DriveResult(await runtime.state(stream_id))
        await runtime.finalize(stream_id)
        state = await runtime.state(stream_id)
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
