from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable

from agent_runtime import (
    AgentRuntime,
    CancelTool,
    Command,
    CommandOutcomeReceived,
    DELIVER_TOOL_NAME,
    DeliveringDecisionMaker,
    InvokeTool,
    LifecycleIntent,
    MemoryJournal,
    ModelDecision,
    OutcomeStatus,
    RuntimeStatus,
    ToolBinding,
    deliver_binding,
    ensure_deliver,
)
from agent_runtime.state import DecisionFrame


DecisionScript = Callable[
    [DecisionFrame],
    ModelDecision | Awaitable[ModelDecision],
]


class SequentialIds:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}-{self._value}"


class ScriptedDecisionMaker:
    def __init__(self, scripts: tuple[DecisionScript, ...]) -> None:
        self.scripts = scripts
        self.frames: list[DecisionFrame] = []

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        script = self.scripts[len(self.frames)]
        self.frames.append(frame)
        decision = script(frame)
        if isinstance(decision, Awaitable):
            return await decision
        return decision


class AgentRuntimeDeliverAdapterTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "deliver-stream"

    def test_command_defaults_follow_effect_kind(self):
        self.assertTrue(Command("c1", InvokeTool("A")).decision_on_outcome)
        self.assertFalse(Command("c2", CancelTool("c1")).decision_on_outcome)
        self.assertFalse(
            Command(
                "c3",
                InvokeTool(DELIVER_TOOL_NAME),
                decision_on_outcome=False,
            ).decision_on_outcome,
        )

    def test_ensure_deliver_appends_invoke_once(self):
        mapped = ensure_deliver(ModelDecision(content="  hello  "))
        self.assertEqual(
            mapped.command_requests,
            (InvokeTool(DELIVER_TOOL_NAME, (("text", "hello"),)),),
        )
        self.assertEqual(mapped.content, "  hello  ")
        self.assertEqual(ensure_deliver(mapped), mapped)
        self.assertEqual(
            ensure_deliver(
                ModelDecision(lifecycle_intent=LifecycleIntent.COMPLETE),
            ).command_requests,
            (),
        )

    def test_ensure_deliver_does_not_duplicate_existing_deliver(self):
        existing = InvokeTool(DELIVER_TOOL_NAME, (("text", "already"),))
        decision = ModelDecision(
            content="ignored extra",
            command_requests=(existing,),
        )
        self.assertEqual(ensure_deliver(decision), decision)

    async def test_content_becomes_delivered_command_then_waits_for_user(self):
        delivered: list[str] = []
        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(content="hello there"),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            DeliveringDecisionMaker(model),
            deliver_binding(delivered.append),
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "hi",
            delivery_id="ask-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        await runtime.dispatcher.wait(step.commands[0].command_id)
        await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)
        outcomes = [
            event.payload
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
        ]

        self.assertEqual(len(model.frames), 1)
        self.assertEqual(step.decision.content, "hello there")
        self.assertEqual(
            step.commands[0].effect,
            InvokeTool(DELIVER_TOOL_NAME, (("text", "hello there"),)),
        )
        self.assertFalse(step.commands[0].decision_on_outcome)
        self.assertEqual(delivered, ["hello there"])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.waiting_for, ("user_message",))

    async def test_complete_waits_for_deliver_then_finalizes(self):
        delivered: list[str] = []
        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="finished",
                lifecycle_intent=LifecycleIntent.COMPLETE,
            ),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            DeliveringDecisionMaker(model),
            deliver_binding(delivered.append),
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "wrap up",
            delivery_id="ask-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        await runtime.dispatcher.wait(step.commands[0].command_id)
        await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)

        self.assertEqual(len(model.frames), 1)
        self.assertEqual(delivered, ["finished"])
        self.assertEqual(state.status, RuntimeStatus.COMPLETED)
        self.assertEqual(state.waiting_for, ())

    async def test_default_tool_outcome_still_requires_a_step(self):
        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="",
                command_requests=(InvokeTool("A"),),
            ),
            lambda _frame: ModelDecision(content="saw the result"),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            DeliveringDecisionMaker(model),
            {
                "A": ToolBinding(_immediate),
                **deliver_binding(lambda _text: None),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="ask-1",
        )
        first = await runtime.advance(self.STREAM_ID)
        await runtime.dispatcher.wait(first.commands[0].command_id)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)

        second = await runtime.advance(self.STREAM_ID)
        await runtime.dispatcher.wait(second.commands[0].command_id)
        await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(len(model.frames), 2)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.waiting_for, ("user_message",))


async def _immediate(_context, _arguments):
    return "A-result"
