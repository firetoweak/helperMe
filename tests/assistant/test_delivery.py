from __future__ import annotations

import unittest

from helperme.assistant.delivery import (
    DELIVER_TOOL_NAME,
    DeliveringDecisionMaker,
    deliver_binding,
    ensure_deliver,
)
from helperme.runtime import (
    AgentRuntime,
    Command,
    CommandOutcomeReceived,
    InvokeTool,
    LifecycleIntent,
    MemoryJournal,
    ModelDecision,
    OutcomeStatus,
    RuntimeStatus,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.session_scheduler import settle_session


class AssistantDeliveryTest(unittest.IsolatedAsyncioTestCase):
    SESSION_ID = "deliver-session"

    def test_command_outcome_decision_defaults_true(self):
        self.assertTrue(Command("c1", InvokeTool("A")).decision_on_outcome)
        self.assertFalse(
            Command(
                "c2",
                InvokeTool("note"),
                decision_on_outcome=False,
            ).decision_on_outcome
        )

    def test_deliver_is_non_deciding_command(self):
        binding = deliver_binding(lambda _text: None)[DELIVER_TOOL_NAME]
        self.assertFalse(binding.decision_on_outcome)

    def test_ensure_deliver_appends_invoke_once(self):
        mapped = ensure_deliver(ModelDecision(content="  hello  "))
        self.assertEqual(
            mapped.command_requests,
            (InvokeTool(DELIVER_TOOL_NAME, (("text", "hello"),)),),
        )
        with self.assertRaisesRegex(ValueError, "product command"):
            ensure_deliver(mapped)
        self.assertEqual(
            ensure_deliver(
                ModelDecision(
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                )
            ).command_requests,
            (),
        )

    async def test_content_is_delivered_and_session_stays_open(self):
        delivered: list[str] = []
        model = ScriptedDecisionMaker(
            (lambda _frame: ModelDecision(content="hello there"),)
        )
        runtime = AgentRuntime(
            MemoryJournal(),
            DeliveringDecisionMaker(model),
            deliver_binding(delivered.append),
            SequentialIds(),
        )
        await runtime.create_session(self.SESSION_ID)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "hi",
            delivery_id="ask-1",
        )

        result = await settle_session(runtime, self.SESSION_ID)
        events = await runtime.snapshot(self.SESSION_ID)
        outcomes = [
            event.payload
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
        ]

        self.assertEqual(delivered, ["hello there"])
        self.assertEqual(outcomes[0].outcome.status, OutcomeStatus.SUCCEEDED)
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(result.state.waiting_for, ("user_message",))

    async def test_complete_intent_does_not_auto_finalize_chat_session(self):
        delivered: list[str] = []
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    content="finished",
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )
        )
        runtime = AgentRuntime(
            MemoryJournal(),
            DeliveringDecisionMaker(model),
            deliver_binding(delivered.append),
            SequentialIds(),
        )
        await runtime.create_session(self.SESSION_ID)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "wrap up",
            delivery_id="ask-1",
        )

        result = await settle_session(runtime, self.SESSION_ID)

        self.assertEqual(delivered, ["finished"])
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
