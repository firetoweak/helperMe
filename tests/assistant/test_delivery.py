from __future__ import annotations

import unittest

from helperme.assistant.delivery import (
    DELIVER_TOOL_NAME,
    PreviewEmitter,
    deliver_binding,
    ensure_deliver,
)
from helperme.runtime import (
    Command,
    InvokeTool,
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
        binding = deliver_binding(
            lambda _session_id, _output_id, _text: None
        )[DELIVER_TOOL_NAME]
        self.assertFalse(binding.decision_on_outcome)

    async def test_preview_is_disposable_and_not_a_delivery(self):
        updates = []
        preview = PreviewEmitter(lambda *values: updates.append(values))

        await preview.start(self.SESSION_ID, "output-1")
        await preview.append(self.SESSION_ID, "output-1", "hel")
        await preview.abort(self.SESSION_ID)

        self.assertEqual(
            updates,
            [
                (self.SESSION_ID, "started", "output-1", None),
                (self.SESSION_ID, "delta", "output-1", "hel"),
                (self.SESSION_ID, "aborted", "output-1", None),
            ],
        )

    async def test_thinking_is_a_parallel_stream_and_survives_preview_abort(self):
        previews = []
        thoughts = []
        preview = PreviewEmitter(
            lambda *values: previews.append(values),
            thinking_sink=lambda *values: thoughts.append(values),
        )

        await preview.start(self.SESSION_ID, "output-1")
        await preview.start_thinking(self.SESSION_ID, "output-1")
        await preview.append_thinking(self.SESSION_ID, "output-1", "想")
        await preview.abort(self.SESSION_ID)
        await preview.finish_thinking(self.SESSION_ID, "output-1")

        self.assertEqual(
            thoughts,
            [
                (self.SESSION_ID, "started", "output-1", None),
                (self.SESSION_ID, "delta", "output-1", "想"),
                (self.SESSION_ID, "finished", "output-1", None),
            ],
        )
        self.assertEqual(previews[-1], (self.SESSION_ID, "aborted", "output-1", None))

    async def test_deliver_routes_its_session_id_to_the_sink(self):
        routed: list[tuple[str, str, str]] = []
        binding = deliver_binding(
            lambda session_id, output_id, text: routed.append(
                (session_id, output_id, text)
            )
        )[DELIVER_TOOL_NAME]

        result = await binding.handler(
            AttemptContext(self.SESSION_ID, "command-1", "attempt-1", 1),
            {"output_id": "output-1", "text": "hello"},
        )

        self.assertEqual(result, "hello")
        self.assertEqual(routed, [(self.SESSION_ID, "output-1", "hello")])

    def test_ensure_deliver_appends_invoke_once(self):
        mapped = ensure_deliver(ModelDecision(content="  hello  "), "output-1")
        self.assertEqual(
            mapped.command_requests,
            (
                InvokeTool(
                    DELIVER_TOOL_NAME,
                    (("output_id", "output-1"), ("text", "hello")),
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "product command"):
            ensure_deliver(mapped, "output-1")
