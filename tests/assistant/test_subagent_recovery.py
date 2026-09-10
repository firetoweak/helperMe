import asyncio
import unittest

from helperme.assistant.subagent import (
    RETURN_FACT, TASK_FACT, persist_return, record_interrupted_return, return_data,
)
from helperme.runtime import (
    AgentRuntime, CommandPhase, DomainFactCommitted, InvokeTool, MemoryJournal,
    ModelDecision, StateProjector, ToolBinding,
)
from tests.assistant.test_runner import ScriptedDecisionMaker


class SubagentRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def interrupted_journal(self, *, child=True):
        async def read(context, arguments):
            await asyncio.Event().wait()

        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            ScriptedDecisionMaker((lambda _: ModelDecision(
                command_requests=(InvokeTool("read_file"),),
            ),)),
            {"read_file": ToolBinding(read)},
        )
        if child:
            await runtime.receive_domain_fact(
                "session", TASK_FACT,
                {"task": "read", "parent_session_id": "parent"},
                source="subagent", delivery_id="task", requests_decision=True,
            )
        else:
            await runtime.receive_user_message("session", "read", delivery_id="task")
        await runtime.advance("session")
        await runtime.dispatcher.close()
        return journal

    async def test_recovery_appends_one_return_without_changing_execution_history(self):
        journal = await self.interrupted_journal()
        before = await journal.snapshot("session")
        await record_interrupted_return(journal, "session")
        after = await journal.snapshot("session")
        self.assertEqual(after[:-1], before)
        self.assertIsInstance(after[-1].payload, DomainFactCommitted)
        self.assertEqual(after[-1].payload.fact_type, RETURN_FACT)
        self.assertFalse(after[-1].payload.data["reported"])
        self.assertIn("执行结果未知", after[-1].payload.data["failure"])
        state = StateProjector().project("session", after).state
        self.assertEqual(state.commands[0].phase, CommandPhase.UNKNOWN)
        await record_interrupted_return(journal, "session")
        self.assertEqual(await journal.snapshot("session"), after)

    async def test_existing_return_wins_over_unknown_attempt(self):
        journal = await self.interrupted_journal()
        await persist_return(journal, "session", return_data(
            "session", reported=True, summary="already reported", failure=None,
        ))
        before = await journal.snapshot("session")
        await record_interrupted_return(journal, "session")
        self.assertEqual(await journal.snapshot("session"), before)

    async def test_top_level_unknown_attempt_is_not_reclaimed(self):
        journal = await self.interrupted_journal(child=False)
        before = await journal.snapshot("session")
        await record_interrupted_return(journal, "session")
        self.assertEqual(await journal.snapshot("session"), before)

    async def test_fresh_child_is_not_reclaimed(self):
        journal = MemoryJournal()
        runtime = AgentRuntime(journal, None, {})
        await runtime.receive_domain_fact(
            "session", TASK_FACT, {"task": "read", "parent_session_id": "parent"},
            source="subagent", delivery_id="task", requests_decision=True,
        )
        before = await journal.snapshot("session")
        await record_interrupted_return(journal, "session")
        self.assertEqual(await journal.snapshot("session"), before)
