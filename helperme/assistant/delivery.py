from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from inspect import isawaitable

from helperme.runtime.dispatcher import AttemptContext, ToolBinding
from helperme.runtime.model import (
    InvokeTool,
    ModelDecision,
)


DELIVER_TOOL_NAME = "deliver"


DeliverySink = Callable[[str, str], Awaitable[None] | None]
"""Route one Session's output. The Host decides where each Session lands."""


def ensure_deliver(decision: ModelDecision) -> ModelDecision:
    """Map assistant text onto an Assistant-owned deliver Command.

    `deliver` is not a model-visible tool. Runtime does not promote
    `decision.content` into a user-visible delivery.
    """

    if any(
        isinstance(request, InvokeTool) and request.name == DELIVER_TOOL_NAME
        for request in decision.command_requests
    ):
        raise ValueError("deliver is a product command, not a model tool")
    text = decision.content.strip()
    if not text:
        return decision
    return replace(
        decision,
        command_requests=decision.command_requests
        + (InvokeTool(DELIVER_TOOL_NAME, (("text", text),)),),
    )


async def emit_delivery(sink: DeliverySink, session_id: str, text: str) -> None:
    emitted = sink(session_id, text)
    if isawaitable(emitted):
        await emitted


def deliver_binding(sink: DeliverySink) -> dict[str, ToolBinding]:
    async def handler(
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> str:
        text = arguments.get("text")
        if type(text) is not str or not text:
            raise ValueError("deliver text must be a non-empty str")
        await emit_delivery(sink, context.session_id, text)
        return text

    return {
        DELIVER_TOOL_NAME: ToolBinding(
            handler,
            decision_on_outcome=False,
        ),
    }
