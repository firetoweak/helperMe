from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

from agent_runtime.dispatcher import AttemptContext, ToolBinding
from agent_runtime.model import (
    InvokeTool,
    ModelDecision,
    RecoveryContract,
    RetrySemantics,
)
from agent_runtime.state import DecisionFrame
from agent_runtime.step import DecisionMaker


DELIVER_TOOL_NAME = "deliver"


def ensure_deliver(decision: ModelDecision) -> ModelDecision:
    """Map assistant text onto a host-owned deliver Command.

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


def deliver_binding(sink: Callable[[str], None]) -> dict[str, ToolBinding]:
    async def handler(
        _context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> str:
        text = arguments.get("text")
        if type(text) is not str or not text:
            raise ValueError("deliver text must be a non-empty str")
        sink(text)
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
