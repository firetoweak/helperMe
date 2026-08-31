from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable

from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.decision import decision_from_llm
from helperme.assistant.runner import SessionScheduler
from helperme.llm.api import LLMProviderError
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
from tests.session_scheduler import SettlingScheduler


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
    async def test_background_failure_is_observable_without_another_wake(self):
        failure = LLMProviderError("provider rejected request")

        async def fail(_frame):
            raise failure

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((fail,)),
            {},
            SequentialIds(),
        )
        scheduler = SessionScheduler(
            runtime,
            control=AssistantControlPlane((), ()),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "hello",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake("session")
            observed = await asyncio.wait_for(
                scheduler.wait_failure(),
                timeout=1,
            )

            self.assertIs(observed, failure)
            self.assertEqual(
                (await runtime.state("session")).status,
                RuntimeStatus.RUNNABLE,
            )
        finally:
            await scheduler.close()

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
        scheduler = SettlingScheduler(runtime)
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
        scheduler = SettlingScheduler(runtime)
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
        scheduler = SettlingScheduler(runtime)
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

    async def test_same_session_wakes_never_overlap_advance(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((lambda _frame: ModelDecision(content="done"),)),
            {},
            SequentialIds(),
        )
        original_advance = runtime.advance
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()
        calls = 0
        active = 0
        maximum_active = 0

        async def tracked_advance(session_id):
            nonlocal calls, active, maximum_active
            calls += 1
            call = calls
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if call == 1:
                    first_started.set()
                    await release_first.wait()
                return await original_advance(session_id)
            finally:
                active -= 1
                if call == 2:
                    second_finished.set()

        runtime.advance = tracked_advance
        scheduler = SessionScheduler(
            runtime,
            control=AssistantControlPlane((), ()),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "hello",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake("session")
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await scheduler.wake("session")
            await asyncio.sleep(0)
            self.assertEqual(maximum_active, 1)

            release_first.set()
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            self.assertEqual(calls, 2)
            self.assertEqual(maximum_active, 1)
        finally:
            release_first.set()
            await scheduler.close()

    async def test_each_activation_calls_advance_once(self):
        second_finished = asyncio.Event()
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(content="first"),
                    lambda _frame: ModelDecision(content="second"),
                )
            ),
            {},
            SequentialIds(),
        )
        original_advance = runtime.advance
        advance_calls = 0

        async def tracked_advance(session_id):
            nonlocal advance_calls
            advance_calls += 1
            try:
                return await original_advance(session_id)
            finally:
                if advance_calls == 2:
                    second_finished.set()

        class ActivationCountingScheduler(SessionScheduler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.activations = 0

            def _start(self, session_id):
                self.activations += 1
                super()._start(session_id)

        runtime.advance = tracked_advance
        scheduler = ActivationCountingScheduler(
            runtime,
            control=AssistantControlPlane((), ()),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "one",
            delivery_id="user-1",
        )
        await runtime.receive_user_message(
            "session",
            "two",
            delivery_id="user-2",
        )

        try:
            await scheduler.wake("session")
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            self.assertEqual(advance_calls, 2)
            self.assertEqual(scheduler.activations, 2)
        finally:
            await scheduler.close()

    async def test_activation_uses_advance_result_without_reading_state(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((lambda _frame: ModelDecision(content="done"),)),
            {},
            SequentialIds(),
        )
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "hello",
            delivery_id="user-1",
        )

        async def forbidden_state(_session_id):
            raise AssertionError("scheduler must not read state after advance")

        runtime.state = forbidden_state
        scheduler = SessionScheduler(
            runtime,
            control=AssistantControlPlane((), ()),
        )
        try:
            should_continue = await scheduler._advance_once("session")
            self.assertFalse(should_continue)
        finally:
            await scheduler.close()
