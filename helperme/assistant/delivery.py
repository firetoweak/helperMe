from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from inspect import isawaitable

from helperme.runtime.dispatcher import AttemptContext, ToolBinding
from helperme.runtime.model import (
    InvokeTool,
    ModelDecision,
    RecoveryContract,
    RetrySemantics,
)
from helperme.runtime.state import DecisionFrame
from helperme.runtime.step import DecisionMaker


DELIVER_TOOL_NAME = "deliver"


DeliverySink = Callable[[str], Awaitable[None] | None]


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
        command_requests=decision.command_requests + (
            InvokeTool(DELIVER_TOOL_NAME, (("text", text),)),
        ),
    )


class DeliveringDecisionMaker:
    def __init__(self, inner: DecisionMaker) -> None:
        self._inner = inner

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        return ensure_deliver(await self._inner.decide(frame))


async def emit_delivery(sink: DeliverySink, text: str) -> None:
    emitted = sink(text)
    if isawaitable(emitted):
        await emitted


def deliver_binding(sink: DeliverySink) -> dict[str, ToolBinding]:
    async def handler(
        _context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> str:
        text = arguments.get("text")
        if type(text) is not str or not text:
            raise ValueError("deliver text must be a non-empty str")
        await emit_delivery(sink, text)
        return text

    return {
        DELIVER_TOOL_NAME: ToolBinding(
            handler,
            recovery=RecoveryContract(
                retry_semantics=RetrySemantics.PROHIBITED,
            ),
            decision_on_outcome=False,
        ),
    }
