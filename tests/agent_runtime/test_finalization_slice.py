from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from agent_runtime import (
    AgentRuntime,
    InvokeTool,
    LifecycleIntent,
    MemoryJournal,
    ModelDecision,
    RuntimeCompleted,
    RuntimeStatus,
    RuntimeTerminated,
    SqliteJournal,
    StepClaimRequest,
    TerminationRequested,
    ToolBinding,
    UserInterruptReceived,
)
from agent_runtime.state import DecisionFrame


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
    def __init__(self, name: str) -> None:
        self.name = name
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.executions: list[Mapping[str, object]] = []

    def binding(self) -> dict[str, ToolBinding]:
        return {self.name: ToolBinding(self._handler)}

    async def _handler(self, _context, arguments: Mapping[str, object]) -> object:
        self.executions.append(arguments)
        self.started.set()
        await self.release.wait()
        return f"{self.name}-result"


def runtime_for(tool: RecordingTool, model: ScriptedDecisionMaker, journal=None):
    return AgentRuntime(
        journal or MemoryJournal(),
        model,
        tool.binding(),
        SequentialIds(),
    )


def terminal_payloads(events):
    return [
        event.payload
        for event in events
        if isinstance(event.payload, (RuntimeCompleted, RuntimeTerminated))
    ]


class AgentRuntimeFinalizationSliceTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "finalization-stream"

    async def test_content_only_waits_for_user_message(self):
        tool = RecordingTool("unused")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(content="done answering"),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "hello",
            delivery_id="ask-1",
        )
        await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)

        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.waiting_for, ("user_message",))
        self.assertEqual(terminal_payloads(events), [])

    async def test_explicit_complete_becomes_completed(self):
        tool = RecordingTool("unused")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="finished",
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "wrap up",
            delivery_id="ask-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)
        completed = terminal_payloads(events)

        self.assertEqual(state.status, RuntimeStatus.COMPLETED)
        self.assertEqual(state.waiting_for, ())
        self.assertEqual(len(completed), 1)
        self.assertIsInstance(completed[0], RuntimeCompleted)
        self.assertEqual(
            completed[0].declared_by_event_id,
            events[-2].event_id,
        )
        rebuilt = await runtime.replay(self.STREAM_ID)
        self.assertEqual(rebuilt.state.status, RuntimeStatus.COMPLETED)
        self.assertEqual(step.decision.lifecycle_intent, LifecycleIntent.COMPLETE)

    async def test_same_step_may_declare_complete_and_start_tools(self):
        tool = RecordingTool("A")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="start then complete",
                    command_requests=(InvokeTool("A"),),
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="ask-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)

        self.assertEqual(step.decision.lifecycle_intent, LifecycleIntent.COMPLETE)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertIn(step.commands[0].command_id, state.waiting_command_ids)
        self.assertEqual(terminal_payloads(events), [])

        tool.release.set()
        await runtime.dispatcher.wait(step.commands[0].command_id)
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(terminal_payloads(events), [])

    async def test_complete_with_open_tool_does_not_finalize(self):
        tool = RecordingTool("A")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="start",
                    command_requests=(InvokeTool("A"),),
                ),
                lambda _frame: ModelDecision(
                    content="done",
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="ask-1",
        )
        first = await runtime.advance(self.STREAM_ID)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "check",
            delivery_id="interrupt-1",
        )
        await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertIn(first.commands[0].command_id, state.waiting_command_ids)
        self.assertEqual(terminal_payloads(events), [])
        tool.release.set()
        await runtime.dispatcher.wait(first.commands[0].command_id)

    async def test_complete_after_abandon_finalizes(self):
        tool = RecordingTool("A")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="start",
                    command_requests=(InvokeTool("A"),),
                ),
                lambda frame: ModelDecision(
                    content="done",
                    abandon_command_ids=(frame.state.commands[0].command.command_id,),
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="ask-1",
        )
        first = await runtime.advance(self.STREAM_ID)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "enough",
            delivery_id="interrupt-1",
        )
        second = await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)

        self.assertEqual(second.decision.lifecycle_intent, LifecycleIntent.COMPLETE)
        self.assertEqual(state.status, RuntimeStatus.COMPLETED)
        self.assertTrue(state.command(first.commands[0].command_id).abandoned)
        tool.release.set()
        await runtime.dispatcher.wait(first.commands[0].command_id)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.COMPLETED)

    async def test_interrupt_stales_completion_declaration(self):
        tool = RecordingTool("unused")
        entered = asyncio.Event()
        release_model = asyncio.Event()

        async def decide_complete(_frame: DecisionFrame) -> ModelDecision:
            entered.set()
            await release_model.wait()
            return ModelDecision(
                content="I think I'm done",
                lifecycle_intent=LifecycleIntent.COMPLETE,
            )

        model = ScriptedDecisionMaker((
            decide_complete,
            lambda _frame: ModelDecision(content="interrupt handled"),
        ))
        runtime = runtime_for(tool, model)
        await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="start-1",
        )
        step_task = asyncio.create_task(runtime.advance(self.STREAM_ID))
        await asyncio.wait_for(entered.wait(), timeout=1)
        interrupt = await runtime.receive_interrupt(
            self.STREAM_ID,
            "wait",
            delivery_id="interrupt-1",
        )
        release_model.set()
        first = await step_task
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)

        self.assertEqual(first.decision.lifecycle_intent, LifecycleIntent.COMPLETE)
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(state.next_trigger_event_id, interrupt.event_id)
        self.assertEqual(terminal_payloads(events), [])

        follow_up = await runtime.advance(self.STREAM_ID)
        self.assertEqual(follow_up.trigger_event_id, interrupt.event_id)
        self.assertIsInstance(
            model.frames[1].trigger_event.payload,
            UserInterruptReceived,
        )
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.WAITING)

    async def test_unfinalized_complete_can_be_recovered(self):
        tool = RecordingTool("unused")
        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="finished",
                lifecycle_intent=LifecycleIntent.COMPLETE,
            ),
        ))
        journal = MemoryJournal()
        runtime = runtime_for(tool, model, journal)
        await runtime.receive_user_message(
            self.STREAM_ID,
            "wrap up",
            delivery_id="ask-1",
        )
        events = await journal.snapshot(self.STREAM_ID)
        frame = runtime.projector.project(self.STREAM_ID, events).next_decision
        self.assertIsNotNone(frame)
        lease = await journal.acquire_step(
            StepClaimRequest(
                stream_id=self.STREAM_ID,
                trigger_event_id=frame.trigger_event.event_id,
                decision_cursor=frame.decision_cursor,
                basis_state_version=frame.basis_state_version,
                observed_journal_position=frame.observed_journal_position,
            ),
            token="step-claim-1",
            owner_id="worker-1",
            lease_seconds=30,
        )
        await runtime.step_runner.commit(frame, lease)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(
            state.steps[-1].decision.lifecycle_intent,
            LifecycleIntent.COMPLETE,
        )

        recovered = AgentRuntime(journal, ScriptedDecisionMaker(()), tool.binding())
        recovered_state = await recovered.state(self.STREAM_ID)
        self.assertEqual(recovered_state.status, RuntimeStatus.WAITING)
        await recovered.recover_once(self.STREAM_ID)
        recovered_state = await recovered.state(self.STREAM_ID)
        events = await journal.snapshot(self.STREAM_ID)
        self.assertEqual(recovered_state.status, RuntimeStatus.COMPLETED)
        self.assertEqual(len(terminal_payloads(events)), 1)

    async def test_later_user_message_does_not_revive_completed_stream(self):
        tool = RecordingTool("unused")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="finished",
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
                lambda _frame: ModelDecision(content="should not run"),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "wrap up",
            delivery_id="ask-1",
        )
        await runtime.advance(self.STREAM_ID)
        await runtime.receive_user_message(
            self.STREAM_ID,
            "one more thing",
            delivery_id="ask-2",
        )
        follow_up = await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)
        self.assertIsNone(follow_up)
        self.assertEqual(state.status, RuntimeStatus.COMPLETED)

    async def test_step_terminate_without_abandon_does_not_finalize(self):
        tool = RecordingTool("A")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="start",
                    command_requests=(InvokeTool("A"),),
                ),
                lambda _frame: ModelDecision(
                    content="give up",
                    lifecycle_intent=LifecycleIntent.TERMINATE,
                ),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="ask-1",
        )
        first = await runtime.advance(self.STREAM_ID)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "stop trying",
            delivery_id="interrupt-1",
        )
        await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertIn(first.commands[0].command_id, state.waiting_command_ids)
        self.assertEqual(terminal_payloads(events), [])
        tool.release.set()
        await runtime.dispatcher.wait(first.commands[0].command_id)

    async def test_step_terminate_after_abandon_finalizes(self):
        tool = RecordingTool("A")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="start",
                    command_requests=(InvokeTool("A"),),
                ),
                lambda frame: ModelDecision(
                    content="give up",
                    abandon_command_ids=(frame.state.commands[0].command.command_id,),
                    lifecycle_intent=LifecycleIntent.TERMINATE,
                ),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="ask-1",
        )
        first = await runtime.advance(self.STREAM_ID)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "give up",
            delivery_id="interrupt-1",
        )
        await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.TERMINATED)
        self.assertIsInstance(terminal_payloads(events)[0], RuntimeTerminated)
        self.assertTrue(state.command(first.commands[0].command_id).abandoned)
        tool.release.set()
        await runtime.dispatcher.wait(first.commands[0].command_id)

    async def test_host_stop_abandons_inflight_work_and_invalidates_claim(self):
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

        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((decide_first,)),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="start-1",
        )
        step_task = asyncio.create_task(runtime.advance(self.STREAM_ID))
        await asyncio.wait_for(entered.wait(), timeout=1)
        stop = await runtime.receive_termination(
            self.STREAM_ID,
            "host stop",
            delivery_id="stop-1",
            source="host",
        )
        release_model.set()
        step = await step_task
        state = await runtime.state(self.STREAM_ID)
        events = await runtime._journal.snapshot(self.STREAM_ID)

        self.assertIsNone(step)
        self.assertIsInstance(stop.payload, TerminationRequested)
        self.assertEqual(state.status, RuntimeStatus.TERMINATED)
        self.assertEqual(len(terminal_payloads(events)), 1)
        self.assertFalse(tool.started.is_set())

        again = await runtime.receive_termination(
            self.STREAM_ID,
            "host stop",
            delivery_id="stop-1",
            source="host",
        )
        self.assertEqual(again.event_id, stop.event_id)

    async def test_host_stop_abandons_already_running_command(self):
        tool = RecordingTool("A")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="start",
                    command_requests=(InvokeTool("A"),),
                ),
            )),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="ask-1",
        )
        first = await runtime.advance(self.STREAM_ID)
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await runtime.receive_termination(
            self.STREAM_ID,
            "stop",
            delivery_id="stop-1",
        )
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.TERMINATED)
        self.assertTrue(state.command(first.commands[0].command_id).abandoned)
        tool.release.set()
        await runtime.dispatcher.wait(first.commands[0].command_id)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.TERMINATED)
        follow_up = await runtime.advance(self.STREAM_ID)
        self.assertIsNone(follow_up)

    async def test_sqlite_competing_finalizers_commit_once(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "journal.sqlite"
        journal = SqliteJournal(path)
        tool = RecordingTool("unused")
        runtime = runtime_for(
            tool,
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="finished",
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
            journal,
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "wrap up",
            delivery_id="ask-1",
        )
        events = await journal.snapshot(self.STREAM_ID)
        frame = runtime.projector.project(self.STREAM_ID, events).next_decision
        lease = await journal.acquire_step(
            StepClaimRequest(
                stream_id=self.STREAM_ID,
                trigger_event_id=frame.trigger_event.event_id,
                decision_cursor=frame.decision_cursor,
                basis_state_version=frame.basis_state_version,
                observed_journal_position=frame.observed_journal_position,
            ),
            token="step-claim-1",
            owner_id="worker-1",
            lease_seconds=30,
        )
        await runtime.step_runner.commit(frame, lease)

        first, second = await asyncio.gather(
            journal.finalize(self.STREAM_ID, "terminal-1"),
            journal.finalize(self.STREAM_ID, "terminal-2"),
        )
        events = await journal.snapshot(self.STREAM_ID)
        terminals = [
            event
            for event in events
            if isinstance(event.payload, RuntimeCompleted)
        ]
        committed = [event for event in (first, second) if event is not None]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(len(committed), 1)
        self.assertEqual(terminals[0].event_id, committed[0].event_id)

        restarted = SqliteJournal(path)
        rebuilt = runtime_for(tool, ScriptedDecisionMaker(()), restarted)
        state = await rebuilt.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.COMPLETED)
