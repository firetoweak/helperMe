from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable

from helperme.assistant.decision import decision_from_llm
from helperme.assistant.runner import SessionScheduler
from helperme.llm.types import LLMResponse, ToolCall
from helperme.runtime import (
    AgentRuntime,
    LifecycleIntent,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    UserMessageReceived,
)
from helperme.runtime.state import DecisionFrame


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


class SessionSchedulerTest(unittest.IsolatedAsyncioTestCase):
    def test_decision_from_llm_maps_text_and_tool_calls(self):
        decision = decision_from_llm(
            LLMResponse(
                content="looking",
                calls=(ToolCall("call-1", "read_file", '{"path":"a.py"}'),),
            ),
            frozenset({"read_file"}),
        )

        self.assertEqual(decision.content, "looking")
        self.assertEqual(decision.lifecycle_intent, LifecycleIntent.NONE)
        self.assertEqual(decision.command_requests[0].name, "read_file")
        self.assertEqual(
            decision.command_requests[0].argument_dict(),
            {"path": "a.py"},
        )

    async def test_user_event_wakes_one_step_and_session_remains_open(self):
        model = ScriptedDecisionMaker((lambda _frame: ModelDecision(content="done"),))
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        scheduler = SessionScheduler(runtime)
        await runtime.create_session("session")

        try:
            await runtime.receive_user_message(
                "session",
                "hello",
                delivery_id="user-1",
            )
            await scheduler.wake("session")
            await scheduler.join()

            state = await runtime.state("session")
            self.assertEqual(len(state.steps), 1)
            self.assertEqual(state.status, RuntimeStatus.WAITING)
        finally:
            await scheduler.close()

    async def test_later_event_is_the_next_step_trigger(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def first(_frame):
            first_started.set()
            await release_first.wait()
            return ModelDecision(content="first")

        model = ScriptedDecisionMaker(
            (
                first,
                lambda _frame: ModelDecision(content="second"),
            )
        )
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        scheduler = SessionScheduler(runtime)
        await runtime.create_session("session")

        try:
            await runtime.receive_user_message(
                "session",
                "one",
                delivery_id="user-1",
            )
            await scheduler.wake("session")
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await runtime.receive_user_message(
                "session",
                "two",
                delivery_id="user-2",
            )
            await scheduler.wake("session")
            release_first.set()
            await asyncio.wait_for(scheduler.join(), timeout=1)

            self.assertEqual(len((await runtime.state("session")).steps), 2)
            messages = [
                frame.trigger_event.payload.content
                for frame in model.frames
                if isinstance(frame.trigger_event.payload, UserMessageReceived)
            ]
            self.assertEqual(messages, ["one", "two"])
        finally:
            await scheduler.close()

    async def test_independent_sessions_advance_concurrently(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()

        async def decide(frame):
            if frame.state.session_id == "session-a":
                first_started.set()
                await release_first.wait()
            else:
                second_finished.set()
            return ModelDecision(content=frame.state.session_id)

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((decide, decide)),
            {},
            SequentialIds(),
        )
        scheduler = SessionScheduler(runtime)
        await runtime.create_session("session-a")
        await runtime.create_session("session-b")
        await runtime.receive_user_message(
            "session-a",
            "one",
            delivery_id="user-a",
        )
        await runtime.receive_user_message(
            "session-b",
            "two",
            delivery_id="user-b",
        )

        try:
            await scheduler.wake("session-a")
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await scheduler.wake("session-b")
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            release_first.set()
            await asyncio.wait_for(scheduler.join(), timeout=1)

            self.assertEqual(len((await runtime.state("session-a")).steps), 1)
            self.assertEqual(len((await runtime.state("session-b")).steps), 1)
        finally:
            release_first.set()
            await scheduler.close()
