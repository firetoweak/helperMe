from __future__ import annotations

import unittest

from helperme.runtime import (
    AgentRuntime,
    LifecycleIntent,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.session_scheduler import SettlingScheduler


class ExplicitFinalizationSliceTest(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_complete_intent_does_not_close_session(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="done",
                        lifecycle_intent=LifecycleIntent.COMPLETE,
                    ),
                )
            ),
            {},
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "hello",
            delivery_id="user-1",
        )
        await scheduler.wake("session")
        await scheduler.join()

        self.assertEqual(
            (await runtime.state("session")).status,
            RuntimeStatus.WAITING,
        )
        await scheduler.close()

    async def test_background_owner_may_finalize_explicitly(self):
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="done",
                        lifecycle_intent=LifecycleIntent.COMPLETE,
                    ),
                )
            ),
            {},
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "work",
            delivery_id="user-1",
        )
        await scheduler.wake("session")
        await scheduler.join()

        terminal = await runtime.finalize("session")

        self.assertIsNotNone(terminal)
        self.assertEqual(
            (await runtime.state("session")).status,
            RuntimeStatus.COMPLETED,
        )
        await scheduler.close()

    async def test_terminal_session_does_not_accept_new_decisions(self):
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    content="done",
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )
        )
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {},
            SequentialIds(),
        )
        scheduler = SettlingScheduler(runtime)
        await runtime.create_session("session")
        await runtime.receive_user_message(
            "session",
            "work",
            delivery_id="user-1",
        )
        await scheduler.wake("session")
        await scheduler.join()
        await runtime.finalize("session")
        await runtime.receive_user_message(
            "session",
            "later",
            delivery_id="user-2",
        )
        await scheduler.wake("session")
        await scheduler.join()

        self.assertEqual(len(model.frames), 1)
        self.assertEqual(
            (await runtime.state("session")).status,
            RuntimeStatus.COMPLETED,
        )
        await scheduler.close()
