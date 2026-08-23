from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from helperme.runtime import (
    AgentRuntime,
    CommandAuthorized,
    CommandPhase,
    CommandRejected,
    DecisionFrame,
    DeliveryIdentity,
    DispatchAttemptStarted,
    EventDraft,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    SqliteJournal,
    ToolBinding,
    UserInterruptReceived,
    UserMessageReceived,
    diagnose_artifacts,
    replay,
)
from helperme.runtime.events import EventPayload


NOW = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)
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


class RecordingTool:
    def __init__(self, name: str, *, requires_authorization: bool = False) -> None:
        self.name = name
        self.requires_authorization = requires_authorization
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.executions: list[Mapping[str, object]] = []

    def binding(self) -> dict[str, ToolBinding]:
        return {
            self.name: ToolBinding(
                self._handler,
                requires_authorization=self.requires_authorization,
            )
        }

    async def _handler(self, _context, arguments: Mapping[str, object]) -> object:
        self.executions.append(arguments)
        self.started.set()
        await self.release.wait()
        return f"{self.name}-result"


def runtime_for(
    tool: RecordingTool,
    model: ScriptedDecisionMaker,
    journal=None,
):
    return AgentRuntime(
        journal or MemoryJournal(),
        model,
        tool.binding(),
        SequentialIds(),
    )


class AgentRuntimeBoundarySliceTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "boundary-stream"

    async def test_unauthorized_command_is_not_claimed_until_granted(self):
        tool = RecordingTool("transfer", requires_authorization=True)
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="request transfer",
                    command_requests=(InvokeTool("transfer"),),
                ),
            )),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "send money",
            delivery_id="ask-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        command_id = step.commands[0].command_id
        state = await runtime.state(self.STREAM_ID)

        self.assertTrue(step.commands[0].requires_authorization)
        self.assertIsNone(state.command(command_id).dispatch_eligible_by_event_id)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.waiting_for, (f"authorization:{command_id}",))
        self.assertFalse(tool.started.is_set())

        denied = await runtime.dispatcher.start_pending(self.STREAM_ID)
        self.assertEqual(denied, ())
        bypass = await runtime.dispatcher._journal.start_attempt(EventDraft(
            event_id="forged-attempt",
            stream_id=self.STREAM_ID,
            payload=DispatchAttemptStarted(
                "forged-attempt",
                command_id,
                1,
                "forged-claim",
                "attacker",
            ),
            occurred_at=NOW,
            causation_id=None,
        ))
        self.assertIsNone(bypass)

        granted = await runtime.grant_command(self.STREAM_ID, command_id)
        self.assertIsInstance(granted.payload, CommandAuthorized)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        dispatched = (await runtime.state(self.STREAM_ID)).command(command_id)
        self.assertEqual(dispatched.phase, CommandPhase.UNKNOWN)
        self.assertEqual(len(dispatched.attempts), 1)
        tool.release.set()
        await runtime.dispatcher.wait(command_id)

    async def test_rejected_command_cannot_be_granted_or_dispatched(self):
        tool = RecordingTool("publish", requires_authorization=True)
        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="request publish",
                command_requests=(InvokeTool("publish"),),
            ),
            lambda _frame: ModelDecision(content="publish rejected"),
        ))
        runtime = runtime_for(tool, model)
        await runtime.receive_user_message(
            self.STREAM_ID,
            "publish this",
            delivery_id="ask-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        command_id = step.commands[0].command_id

        rejected = await runtime.reject_command(self.STREAM_ID, command_id)
        self.assertIsInstance(rejected.payload, CommandRejected)
        self.assertIsNone(
            await runtime.grant_command(self.STREAM_ID, command_id)
        )
        self.assertFalse(tool.started.is_set())

        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(state.next_trigger_event_id, rejected.event_id)
        self.assertEqual(
            state.command(command_id).authorization_rejected_by_event_id,
            rejected.event_id,
        )
        self.assertNotIn(command_id, state.waiting_command_ids)

        follow_up = await runtime.advance(self.STREAM_ID)
        self.assertEqual(follow_up.trigger_event_id, rejected.event_id)
        self.assertIsInstance(
            model.frames[1].trigger_event.payload,
            CommandRejected,
        )
        self.assertFalse(tool.started.is_set())

    async def test_interrupt_is_not_skipped_or_swallowed_by_older_step(self):
        tool = RecordingTool("A")
        entered = asyncio.Event()
        release_model = asyncio.Event()

        async def decide_first(_frame: DecisionFrame) -> ModelDecision:
            entered.set()
            await release_model.wait()
            return ModelDecision(
                content="keep going",
                command_requests=(InvokeTool("A"),),
            )

        model = ScriptedDecisionMaker((
            decide_first,
            lambda _frame: ModelDecision(content="interrupt handled"),
        ))
        runtime = runtime_for(tool, model)
        user_message = await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="start-1",
        )
        step_task = asyncio.create_task(runtime.advance(self.STREAM_ID))
        await asyncio.wait_for(entered.wait(), timeout=1)
        interrupt = await runtime.receive_interrupt(
            self.STREAM_ID,
            "stop",
            delivery_id="interrupt-1",
        )
        release_model.set()
        step = await step_task

        self.assertEqual(step.trigger_event_id, user_message.event_id)
        self.assertNotIn(interrupt.event_id, model.frames[0].state.visible_event_ids)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(state.next_trigger_event_id, interrupt.event_id)
        consumed = model.frames[0].state.consumed_trigger_event_ids
        self.assertNotIn(interrupt.event_id, consumed)

        follow_up = await runtime.advance(self.STREAM_ID)
        self.assertEqual(follow_up.trigger_event_id, interrupt.event_id)
        self.assertIsInstance(
            model.frames[1].trigger_event.payload,
            UserInterruptReceived,
        )
        self.assertIn(interrupt.event_id, model.frames[1].state.visible_event_ids)
        self.assertIn(
            step.step_id,
            {item.step_id for item in model.frames[1].state.prior_steps},
        )
        tool.release.set()
        await runtime.dispatcher.wait(step.commands[0].command_id)

    async def test_decision_content_is_not_delivered_user_message(self):
        tool = RecordingTool("deliver")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="任务完成",
                    command_requests=(
                        InvokeTool("deliver", (("text", "hello user"),)),
                    ),
                ),
            )),
        )
        inbound = await runtime.receive_user_message(
            self.STREAM_ID,
            "say hello",
            delivery_id="ask-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        tool.release.set()
        await runtime.dispatcher.wait(step.commands[0].command_id)

        turn = await runtime.turn(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)
        self.assertEqual(
            [item.event_id for item in turn.user_messages],
            [inbound.event_id],
        )
        self.assertEqual(turn.user_messages[0].content, "say hello")
        self.assertEqual(step.decision.content, "任务完成")
        self.assertEqual(tool.executions, [{"text": "hello user"}])
        self.assertEqual(
            [
                event.event_id
                for event in events
                if isinstance(event.payload, UserMessageReceived)
            ],
            [inbound.event_id],
        )

    async def test_preview_tokens_cannot_enter_the_journal(self):
        with self.assertRaises(TypeError):
            EventDraft(
                event_id="preview-1",
                stream_id=self.STREAM_ID,
                payload="streaming token",
                occurred_at=NOW,
            )
        payload_names = {
            getattr(item, "__name__", str(item))
            for item in EventPayload.__args__
        }
        self.assertNotIn("StreamPreviewReceived", payload_names)
        self.assertIn("StepCommitted", payload_names)
        self.assertIn("CommandAuthorized", payload_names)

    async def test_missing_artifact_degrades_replay_without_substitution(self):
        digest = "a" * 64
        journal = MemoryJournal()
        await journal.accept_delivery(EventDraft(
            event_id="message-1",
            stream_id=self.STREAM_ID,
            payload=UserMessageReceived("read this"),
            occurred_at=NOW,
            artifact_refs=(digest,),
            delivery=DeliveryIdentity("user", "ask-1"),
        ))
        events = await journal.snapshot(self.STREAM_ID)
        uninspected = diagnose_artifacts(events)
        rebuilt = replay(self.STREAM_ID, events)
        inspected_empty = diagnose_artifacts(events, available_refs=())

        self.assertEqual(uninspected.refs, (digest,))
        self.assertEqual(uninspected.missing, ())
        self.assertFalse(uninspected.inspected)
        self.assertFalse(uninspected.complete)
        self.assertEqual(rebuilt.turn.user_messages[0].content, "read this")
        self.assertEqual(rebuilt.trace.entries[0].kind, "UserMessageReceived")
        self.assertFalse(rebuilt.artifacts.inspected)
        self.assertEqual(rebuilt.artifacts.missing, ())
        self.assertTrue(inspected_empty.inspected)
        self.assertEqual(inspected_empty.missing, (digest,))
        self.assertFalse(inspected_empty.complete)
        self.assertTrue(
            diagnose_artifacts(events, available_refs=(digest,)).complete
        )

    async def test_sqlite_grant_is_single_winner_and_survives_restart(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "boundary.db"
        first = SqliteJournal(path)
        tool = RecordingTool("sensitive", requires_authorization=True)
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="need grant",
                    command_requests=(InvokeTool("sensitive"),),
                ),
            )),
            journal=first,
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "do it",
            delivery_id="ask-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        command_id = step.commands[0].command_id
        issued = (await first.snapshot(self.STREAM_ID))[-1]

        journal_a = SqliteJournal(path)
        journal_b = SqliteJournal(path)
        draft_a = EventDraft(
            event_id="grant-a",
            stream_id=self.STREAM_ID,
            payload=CommandAuthorized(command_id),
            occurred_at=NOW,
            causation_id=issued.event_id,
        )
        draft_b = EventDraft(
            event_id="grant-b",
            stream_id=self.STREAM_ID,
            payload=CommandAuthorized(command_id),
            occurred_at=NOW,
            causation_id=issued.event_id,
        )
        winners = await asyncio.gather(
            journal_a.grant_command(draft_a),
            journal_b.grant_command(draft_b),
        )
        self.assertEqual(sum(event is not None for event in winners), 1)

        reopened = SqliteJournal(path)
        events = await reopened.snapshot(self.STREAM_ID)
        rebuilt = replay(self.STREAM_ID, events)
        self.assertIsNotNone(
            rebuilt.state.command(command_id).dispatch_eligible_by_event_id
        )
        self.assertIsNone(
            await reopened.grant_command(EventDraft(
                event_id="grant-c",
                stream_id=self.STREAM_ID,
                payload=CommandAuthorized(command_id),
                occurred_at=NOW,
                causation_id=issued.event_id,
            ))
        )
