from __future__ import annotations

import asyncio
import unittest

from helperme.runtime import (
    AgentRuntime,
    CommandPhase,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    ToolBinding,
    UserMessageReceived,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.session_scheduler import SettlingScheduler


class RuntimeSemanticSliceTest(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_command_group_waits_for_all_outcomes(self):
        left = asyncio.Event()
        right = asyncio.Event()

        async def wait_left(_context, _arguments):
            await left.wait()
            return "left"

        async def wait_right(_context, _arguments):
            await right.wait()
            return "right"

        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool("left"),
                        InvokeTool("right"),
                    )
                ),
                lambda _frame: ModelDecision(content="both done"),
            )
        )
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {
                "left": ToolBinding(wait_left),
                "right": ToolBinding(wait_right),
            },
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await scheduler.wake("session")
        await asyncio.sleep(0)

        left.set()
        await asyncio.sleep(0)
        self.assertEqual(len(model.frames), 1)
        right.set()
        await scheduler.join()

        state = await runtime.state("session")
        self.assertEqual(len(model.frames), 2)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        await scheduler.close()

    async def test_later_user_message_preserves_event_order(self):
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(content="first"),
                lambda _frame: ModelDecision(content="second"),
            )
        )
        runtime = AgentRuntime(MemoryJournal(), model, {}, SequentialIds())
        scheduler = SettlingScheduler(runtime)
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
        await scheduler.wake("session")
        await scheduler.join()

        messages = [
            frame.trigger_event.payload.content
            for frame in model.frames
            if isinstance(frame.trigger_event.payload, UserMessageReceived)
        ]
        self.assertEqual(messages, ["one", "two"])
        await scheduler.close()

    async def test_tool_error_is_not_wrapped_and_attempt_stays_unknown(self):
        async def explode(_context, _arguments):
            raise LookupError("raw failure")

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(InvokeTool("explode"),),
                    ),
                )
            ),
            {"explode": ToolBinding(explode)},
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "go",
            delivery_id="user-1",
        )
        await scheduler.wake("session")

        with self.assertRaisesRegex(LookupError, "raw failure"):
            await scheduler.join()
        state = await runtime.state("session")
        self.assertEqual(
            state.commands[0].phase,
            CommandPhase.UNKNOWN,
        )
        await scheduler.close()
