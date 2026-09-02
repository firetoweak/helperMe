from __future__ import annotations

import unittest

from helperme.assistant.delivery import (
    DELIVER_TOOL_NAME,
    deliver_binding,
    ensure_deliver,
)
from helperme.runtime import (
    Command,
    InvokeTool,
    LifecycleIntent,
    ModelDecision,
)
from helperme.runtime.dispatcher import AttemptContext


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
        binding = deliver_binding(lambda _session_id, _text: None)[DELIVER_TOOL_NAME]
        self.assertFalse(binding.decision_on_outcome)

    async def test_deliver_routes_its_session_id_to_the_sink(self):
        routed: list[tuple[str, str]] = []
        binding = deliver_binding(
            lambda session_id, text: routed.append((session_id, text))
        )[DELIVER_TOOL_NAME]

        result = await binding.handler(
            AttemptContext(self.SESSION_ID, "command-1", "attempt-1", 1),
            {"text": "hello"},
        )

        self.assertEqual(result, "hello")
        self.assertEqual(routed, [(self.SESSION_ID, "hello")])

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
