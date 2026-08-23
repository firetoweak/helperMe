from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from helperme.assistant.artifacts import (
    FileArtifactStore,
    MemoryArtifactGateway,
    read_artifact_binding,
)
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.context.projection import (
    ModelContextBudgetExceeded,
    ModelContextProjector,
    ModelContextSettings,
    externalize_payload,
    parse_tool_result_meta,
    project_chat_messages,
)
from helperme.assistant.decision import bind_executor_tools
from helperme.assistant.runner import drive_until_idle
from helperme.runtime import (
    AgentRuntime,
    CommandOutcome,
    CommandOutcomeReceived,
    Event,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    OutcomeStatus,
    ToolBinding,
)
from helperme.runtime.dispatcher import AttemptContext, ToolTerminal
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds


class ArtifactBoundaryTest(unittest.TestCase):
    def test_file_store_rejects_artifact_path_traversal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            store = FileArtifactStore(root)

            with self.assertRaises(ValueError):
                store.read("../../outside", 0, 10)

            self.assertFalse((Path(directory) / "outside.json").exists())


class CharacterEstimator:
    def estimate(self, messages: list, tools: list) -> int:
        return len(
            json.dumps(
                {"messages": messages, "tools": tools},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def calibrate(self, messages, tools, actual_input_tokens):
        return None


def _deliver(text: str) -> ModelDecision:
    return ModelDecision(
        content=text,
        command_requests=(
            InvokeTool(DELIVER_TOOL_NAME, (("text", text),)),
        ),
    )


class ModelContextProjectorTest(unittest.IsolatedAsyncioTestCase):
    STREAM = "ctx-stream"

    def _projector(self, **overrides) -> ModelContextProjector:
        gateway = overrides.pop("gateway", MemoryArtifactGateway())
        settings = ModelContextSettings(
            recent_protection_tokens=overrides.pop(
                "recent_protection_tokens",
                8,
            ),
            size_externalize_chars=overrides.pop("size_externalize_chars", 10_000),
            preview_chars=overrides.pop("preview_chars", 10),
            context_limit=overrides.pop("context_limit", 200_000),
            input_budget_ratio=overrides.pop("input_budget_ratio", 0.75),
        )
        return ModelContextProjector(
            gateway=gateway,
            settings=settings,
            estimator=CharacterEstimator(),
            **overrides,
        )

    def test_projection_rejects_outcome_without_visible_command(self):
        outcome = Event(
            event_id="outcome-1",
            stream_id=self.STREAM,
            sequence=1,
            payload=CommandOutcomeReceived(
                "missing-command",
                "attempt-1",
                CommandOutcome(OutcomeStatus.SUCCEEDED, value="done"),
            ),
            occurred_at=datetime.now(timezone.utc),
            causation_id=None,
            correlation_id=None,
            schema_version=2,
            artifact_refs=(),
        )

        with self.assertRaises(KeyError):
            project_chat_messages(
                (outcome,),
                (outcome.event_id,),
                "sys",
            )

    async def _history(self, scripts, tools, users: tuple[str, ...]):
        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(scripts),
            {**tools, **deliver_binding(delivered.append)},
            SequentialIds(),
        )
        for index, text in enumerate(users, start=1):
            await runtime.receive_user_message(
                self.STREAM,
                text,
                delivery_id=f"ask-{index}",
            )
            await drive_until_idle(runtime, self.STREAM)
        events = await runtime._journal.snapshot(self.STREAM)
        return events, delivered

    def _tool_messages(self, messages):
        return [message for message in messages if message["role"] == "tool"]

    async def test_raw_projection_keeps_full_tool_body(self):
        async def ping(_context, _arguments):
            return "pong-body"

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("done"),
            ),
            {"ping": ToolBinding(ping)},
            ("go",),
        )
        messages = project_chat_messages(
            events,
            tuple(event.event_id for event in events),
            "sys",
        )
        self.assertIn("pong-body", self._tool_messages(messages)[0]["content"])

    async def test_previous_user_consumed_success_is_dehydrated(self):
        async def ping(_context, _arguments):
            return "old-result"

        gateway = MemoryArtifactGateway()
        events, delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {"ping": ToolBinding(ping)},
            ("first", "second"),
        )
        prepared = self._projector(gateway=gateway).prepare(
            events,
            tuple(event.event_id for event in events),
            self.STREAM,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        externalized, artifact_id = parse_tool_result_meta(tool["content"])
        self.assertEqual(delivered, ["first-done", "second-done"])
        self.assertTrue(externalized)
        self.assertIsNotNone(artifact_id)
        self.assertIn(tool["tool_call_id"], prepared.age_dehydrated_command_ids)
        self.assertNotIn("old-result", tool["content"])
        chunk = gateway.for_stream(self.STREAM).read(artifact_id, 0, 3000)
        self.assertIn("old-result", chunk.content)

    async def test_parallel_dehydration_ignores_outcome_arrival_order(self):
        release_slow = asyncio.Event()

        async def slow(_context, _arguments):
            await release_slow.wait()
            await asyncio.sleep(0.02)
            return "slow-result"

        async def fast(_context, _arguments):
            release_slow.set()
            return "fast-result"

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(
                        InvokeTool("slow"),
                        InvokeTool("fast"),
                    ),
                ),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {
                "slow": ToolBinding(slow),
                "fast": ToolBinding(fast),
            },
            ("first", "second"),
        )

        prepared = self._projector().prepare(
            events,
            tuple(event.event_id for event in events),
            self.STREAM,
            "sys",
        )

        self.assertEqual(len(prepared.age_dehydrated_command_ids), 2)
        assistant_call = next(
            message
            for message in prepared.messages
            if message["role"] == "assistant" and "tool_calls" in message
        )
        tool_messages = self._tool_messages(prepared.messages)[:2]
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            [call["id"] for call in assistant_call["tool_calls"]],
        )
        for message in tool_messages:
            externalized, _ = parse_tool_result_meta(message["content"])
            self.assertTrue(externalized)

    async def test_latest_user_turn_is_not_age_dehydrated(self):
        async def ping(_context, _arguments):
            return "fresh-result"

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("done"),
            ),
            {"ping": ToolBinding(ping)},
            ("go",),
        )
        prepared = self._projector().prepare(
            events,
            tuple(event.event_id for event in events),
            self.STREAM,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        self.assertIn("fresh-result", tool["content"])
        self.assertEqual(prepared.age_dehydrated_command_ids, ())

    async def test_failed_result_before_latest_user_stays(self):
        async def boom(_context, _arguments):
            return ToolTerminal(
                CommandOutcome(
                    OutcomeStatus.FAILED,
                    error_type="Boom",
                    error_message="failed-on-purpose",
                ),
            )

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("boom"),),
                ),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {"boom": ToolBinding(boom)},
            ("first", "second"),
        )
        prepared = self._projector().prepare(
            events,
            tuple(event.event_id for event in events),
            self.STREAM,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        self.assertIn("failed-on-purpose", tool["content"])
        self.assertEqual(prepared.age_dehydrated_command_ids, ())

    async def test_result_consumed_before_latest_user_stays(self):
        async def ping(_context, _arguments):
            return "pending-result"

        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: ModelDecision(content="observed result"),
                lambda _frame: _deliver("after-new-user"),
            )),
            {
                "ping": ToolBinding(ping),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM,
            "first",
            delivery_id="ask-1",
        )
        await runtime.advance(self.STREAM)
        await runtime.dispatcher.wait_all()
        await runtime.finalize(self.STREAM)
        await runtime.receive_user_message(
            self.STREAM,
            "second",
            delivery_id="ask-2",
        )
        await drive_until_idle(runtime, self.STREAM)
        events = await runtime._journal.snapshot(self.STREAM)
        prepared = self._projector().prepare(
            events,
            tuple(event.event_id for event in events),
            self.STREAM,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        self.assertIn("pending-result", tool["content"])
        self.assertEqual(prepared.age_dehydrated_command_ids, ())
        self.assertEqual(delivered, ["after-new-user"])

    async def test_oversized_result_in_protection_window_is_stubbed(self):
        blob = "N" * 200

        async def ping(_context, _arguments):
            return blob

        gateway = MemoryArtifactGateway()
        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("done"),
            ),
            {"ping": ToolBinding(ping)},
            ("go",),
        )
        prepared = self._projector(
            gateway=gateway,
            size_externalize_chars=80,
        ).prepare(
            events,
            tuple(event.event_id for event in events),
            self.STREAM,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        _, artifact_id = parse_tool_result_meta(tool["content"])
        self.assertIsNotNone(artifact_id)
        self.assertNotIn(blob, tool["content"])
        self.assertIn(
            tool["tool_call_id"],
            prepared.size_externalized_command_ids,
        )
        chunk = gateway.for_stream(self.STREAM).read(artifact_id, 0, 3000)
        self.assertIn(blob, chunk.content)

    async def test_token_window_can_keep_older_consumed_result(self):
        async def ping(_context, _arguments):
            return "keep-me"

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {"ping": ToolBinding(ping)},
            ("first", "second"),
        )
        prepared = self._projector(
            recent_protection_tokens=1_000_000,
        ).prepare(
            events,
            tuple(event.event_id for event in events),
            self.STREAM,
            "sys",
        )
        tool = self._tool_messages(prepared.messages)[0]
        self.assertIn("keep-me", tool["content"])
        self.assertEqual(prepared.age_dehydrated_command_ids, ())

    async def test_budget_overflow_fails_fast(self):
        async def ping(_context, _arguments):
            return "old-result"

        events, _delivered = await self._history(
            (
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(InvokeTool("ping"),),
                ),
                lambda _frame: _deliver("first-done"),
                lambda _frame: _deliver("second-done"),
            ),
            {"ping": ToolBinding(ping)},
            ("first", "second"),
        )
        projector = self._projector(context_limit=20, input_budget_ratio=0.5)
        with self.assertRaises(ModelContextBudgetExceeded):
            projector.prepare(
                events,
                tuple(event.event_id for event in events),
                self.STREAM,
                "sys",
            )

    async def test_execute_time_externalize_writes_stub_value(self):
        gateway = MemoryArtifactGateway()
        settings = ModelContextSettings(
            size_externalize_chars=40,
            preview_chars=8,
        )

        class _Runner:
            def names(self):
                return ("blob",)

            def requires_authorization(self, _name):
                return False

            async def execute(self, _name, _arguments):
                return {"ok": True, "code": "OK", "data": "Z" * 80}

        bindings = bind_executor_tools(_Runner(), gateway, settings)
        result = await bindings["blob"].handler(
            AttemptContext("s1", "cmd-1", "att-1", 1, None),
            {},
        )
        self.assertEqual(result["externalized"], True)
        chunk = gateway.for_stream("s1").read(result["artifact_id"], 0, 3000)
        self.assertIn("Z" * 80, chunk.content)

    async def test_read_artifact_binding_pages_stream_store(self):
        gateway = MemoryArtifactGateway()
        artifact = gateway.for_stream("s1").save("abcdef")
        binding = read_artifact_binding(gateway)["read_artifact"]
        first = await binding.handler(
            AttemptContext("s1", "cmd-1", "att-1", 1, None),
            {"artifact_id": artifact.artifact_id, "offset": 0, "limit": 3},
        )
        self.assertEqual(first["data"]["content"], "abc")
        self.assertEqual(first["data"]["next_offset"], 3)
        missing = await binding.handler(
            AttemptContext("s2", "cmd-1", "att-1", 1, None),
            {"artifact_id": artifact.artifact_id, "offset": 0, "limit": 3},
        )
        self.assertEqual(missing["code"], "ARTIFACT_NOT_FOUND")

    def test_externalize_payload_below_threshold_is_identity(self):
        gateway = MemoryArtifactGateway()
        payload, artifact_id = externalize_payload(
            {"ok": True},
            gateway.for_stream("s1"),
            max_chars=80,
            preview_chars=10,
        )
        self.assertEqual(payload, {"ok": True})
        self.assertIsNone(artifact_id)
