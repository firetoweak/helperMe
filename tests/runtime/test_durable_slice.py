from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helperme.assistant.runner import SessionScheduler
from helperme.runtime import (
    AgentRuntime,
    CommandPhase,
    InvokeTool,
    ModelDecision,
    SqliteJournal,
    ToolBinding,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


class DurableRuntimeSliceTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_session_identity_is_idempotent_and_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            journal = SqliteJournal(path)
            self.assertTrue(await journal.create_session("session"))
            self.assertFalse(await journal.create_session("session"))
            self.assertTrue(await SqliteJournal(path).session_exists("session"))

    async def test_delivery_deduplicates_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            first = SqliteJournal(path)
            await first.create_session("session")
            runtime = AgentRuntime(
                first,
                ScriptedDecisionMaker((lambda _frame: ModelDecision(content="one"),)),
                {},
                SequentialIds(),
            )
            event = await runtime.receive_user_message(
                "session",
                "hello",
                delivery_id="delivery-1",
            )

            restarted = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {},
                SequentialIds(),
            )
            duplicate = await restarted.receive_user_message(
                "session",
                "hello",
                delivery_id="delivery-1",
            )

            self.assertEqual(duplicate.event_id, event.event_id)
            self.assertEqual(len(await restarted.snapshot("session")), 1)

    async def test_step_and_command_outcome_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            model = ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        command_requests=(InvokeTool("echo"),),
                    ),
                    lambda _frame: ModelDecision(content="done"),
                )
            )
            runtime = AgentRuntime(
                SqliteJournal(path),
                model,
                {"echo": ToolBinding(_echo)},
                SequentialIds(),
            )
            scheduler = SessionScheduler(runtime)
            await runtime.create_session("session")
            try:
                await runtime.receive_user_message(
                    "session",
                    "go",
                    delivery_id="delivery-1",
                )
                await scheduler.wake("session")
                await scheduler.join()
            finally:
                await scheduler.close()

            restarted = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {"echo": ToolBinding(_echo)},
                SequentialIds(),
            )
            state = await restarted.state("session")
            self.assertEqual(len(state.steps), 2)
            self.assertTrue(
                all(
                    command.phase is CommandPhase.TERMINAL for command in state.commands
                )
            )

    async def test_failed_unrecorded_attempt_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            runtime = AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            command_requests=(InvokeTool("explode"),),
                        ),
                    )
                ),
                {"explode": ToolBinding(_explode)},
                SequentialIds(),
            )
            scheduler = SessionScheduler(runtime)
            await runtime.create_session("session")
            await runtime.receive_user_message(
                "session",
                "go",
                delivery_id="delivery-1",
            )
            await scheduler.wake("session")
            with self.assertRaisesRegex(RuntimeError, "boom"):
                await scheduler.join()
            await scheduler.close()

            state = await AgentRuntime(
                SqliteJournal(path),
                ScriptedDecisionMaker(()),
                {"explode": ToolBinding(_explode)},
                SequentialIds(),
            ).state("session")
            self.assertEqual(
                state.commands[0].phase,
                CommandPhase.UNKNOWN,
            )


async def _echo(_context, _arguments):
    return "ok"


async def _explode(_context, _arguments):
    raise RuntimeError("boom")
