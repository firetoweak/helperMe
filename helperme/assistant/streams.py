from __future__ import annotations

from dataclasses import dataclass

from helperme.assistant.completion.judgment import JudgmentPolicy
from helperme.assistant.runner import (
    StreamNotFoundError,
    drive_until_idle,
    pending_authorization_ids,
    resume_stream,
)
from helperme.assistant.toolsets import ToolSurface
from helperme.runtime import AgentRuntime, RuntimeStatus
from helperme.runtime.model import CanonicalState


@dataclass(frozen=True, slots=True)
class StreamView:
    status: str
    waiting_for: tuple[str, ...]
    pending_authorization_ids: tuple[str, ...]
    terminal: bool
    should_drive: bool


def stream_view(state: CanonicalState) -> StreamView:
    terminal = state.status in {
        RuntimeStatus.COMPLETED,
        RuntimeStatus.TERMINATED,
    }
    return StreamView(
        status=state.status.value,
        waiting_for=state.waiting_for,
        pending_authorization_ids=pending_authorization_ids(state),
        terminal=terminal,
        should_drive=(
            not terminal
            and (
                state.status is RuntimeStatus.RUNNABLE
                or bool(state.waiting_command_ids)
            )
        ),
    )


class AssistantStreams:
    """给定 Stream identity 后的 Assistant 应用操作。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        surface: ToolSurface,
        *,
        policy: JudgmentPolicy | None = None,
    ) -> None:
        self._runtime = runtime
        self._surface = surface
        self._policy = policy

    async def create(self, stream_id: str) -> StreamView:
        created = await self._runtime.create_stream(stream_id)
        if not created:
            raise ValueError(f"Stream 已存在: {stream_id}")
        return await self.view(stream_id)

    async def resume(self, stream_id: str) -> StreamView:
        state = await resume_stream(self._runtime, self._surface, stream_id)
        return stream_view(state)

    async def view(self, stream_id: str) -> StreamView:
        return stream_view(await self._runtime.state(stream_id))

    async def receive_user_message(
        self,
        stream_id: str,
        content: str,
        *,
        delivery_id: str,
    ) -> None:
        await self._runtime.receive_user_message(
            stream_id,
            content,
            delivery_id=delivery_id,
        )
        if self._policy is not None:
            await self._policy.on_user_message(
                self._runtime,
                stream_id,
                content,
            )

    async def receive_interrupt(
        self,
        stream_id: str,
        reason: str,
        *,
        delivery_id: str,
    ) -> None:
        await self._runtime.receive_interrupt(
            stream_id,
            reason,
            delivery_id=delivery_id,
        )
        await self._runtime.advance(stream_id)

    async def request_termination(
        self,
        stream_id: str,
        reason: str,
        *,
        delivery_id: str,
    ) -> None:
        await self._runtime.receive_termination(
            stream_id,
            reason,
            delivery_id=delivery_id,
        )
        await self._runtime.advance(stream_id)

    async def resolve_authorizations(
        self,
        stream_id: str,
        *,
        approved: bool,
    ) -> None:
        state = await self._runtime.state(stream_id)
        for command_id in pending_authorization_ids(state):
            if approved:
                await self._runtime.grant_command(stream_id, command_id)
            else:
                await self._runtime.reject_command(stream_id, command_id)

    async def drive(
        self,
        stream_id: str,
        *,
        evaluate_completion: bool = True,
    ) -> StreamView:
        result = await drive_until_idle(
            self._runtime,
            stream_id,
            policy=self._policy if evaluate_completion else None,
        )
        return stream_view(result.state)
