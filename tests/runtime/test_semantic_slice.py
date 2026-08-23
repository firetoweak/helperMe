from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from helperme.runtime.codec import EVENT_SCHEMA_VERSION
from helperme.runtime import (
    AgentRuntime,
    CancelTool,
    CancellationContract,
    Command,
    CommandOutcomeReceived,
    CommandPhase,
    DecisionFrame,
    DeliveryIdentity,
    DispatchAttemptStarted,
    Event,
    EventDraft,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    OutcomeStatus,
    RuntimeStatus,
    Step,
    StepCommitted,
    StateProjector,
    StepClaimRequest,
    ToolBinding,
    UserInterruptReceived,
    UserMessageReceived,
    replay,
)


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


class ControlledTools:
    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        self.started_names: set[str] = set()
        self.all_started = asyncio.Event()
        self.started = {name: asyncio.Event() for name in names}
        self.release = {name: asyncio.Event() for name in names}
        self.cancelled = {name: asyncio.Event() for name in names}
        self.executions: list[str] = []

    def handlers(self) -> dict[str, ToolBinding]:
        return {
            name: ToolBinding(
                self._handler(name),
                cancellation=(
                    CancellationContract.TASK_CANCELLED_ERROR_CONFIRMS
                ),
            )
            for name in self.names
        }

    def _handler(
        self,
        name: str,
    ) -> Callable[[Mapping[str, object]], Awaitable[object]]:
        async def execute(
            _context,
            _arguments: Mapping[str, object],
        ) -> object:
            self.executions.append(name)
            self.started_names.add(name)
            self.started[name].set()
            if self.started_names == set(self.names):
                self.all_started.set()
            try:
                await self.release[name].wait()
            except asyncio.CancelledError:
                self.cancelled[name].set()
                raise
            return f"{name}-result"

        return execute


class PausingOutcomeJournal(MemoryJournal):
    def __init__(self) -> None:
        super().__init__()
        self.pause_next_outcome = False
        self.outcome_append_started = asyncio.Event()
        self.release_outcome_append = asyncio.Event()

    async def record_attempt_fact(self, draft):
        if (
            self.pause_next_outcome
            and isinstance(draft.payload, CommandOutcomeReceived)
        ):
            self.pause_next_outcome = False
            self.outcome_append_started.set()
            await self.release_outcome_append.wait()
        return await super().record_attempt_fact(draft)


def initial_tool_decision(_frame: DecisionFrame) -> ModelDecision:
    return ModelDecision(
        content="start A, B, C",
        command_requests=(
            InvokeTool("A"),
            InvokeTool("B"),
            InvokeTool("C"),
        ),
    )


def tool_command_ids(step) -> dict[str, str]:
    return {
        command.effect.name: command.command_id
        for command in step.commands
        if isinstance(command.effect, InvokeTool)
    }


def outcome_events(events):
    return [
        event
        for event in events
        if isinstance(event.payload, CommandOutcomeReceived)
    ]


@dataclass
class AbandonScenario:
    runtime: AgentRuntime
    journal: MemoryJournal
    decision_maker: ScriptedDecisionMaker
    tools: ControlledTools
    command_ids: dict[str, str]
    step_2: object


class AgentRuntimeSemanticSliceTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "stream-1"

    async def test_parallel_tool_group_is_unordered_and_waits_until_terminal(self):
        tools = ControlledTools(("A", "B", "C"))
        model = ScriptedDecisionMaker((
            initial_tool_decision,
            lambda frame: ModelDecision(content="all three ready"),
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            tools.handlers(),
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "collect",
            delivery_id="collect-1",
        )
        step_1 = await runtime.advance(self.STREAM_ID)
        self.assertIsNotNone(step_1)
        command_ids = tool_command_ids(step_1)

        await asyncio.wait_for(tools.all_started.wait(), timeout=1)
        self.assertEqual(tools.started_names, {"A", "B", "C"})

        tools.release["B"].set()
        await runtime.dispatcher.wait(command_ids["B"])
        self.assertIsNone(await runtime.advance(self.STREAM_ID))
        self.assertEqual(len(model.frames), 1)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(
            set(state.waiting_command_ids),
            {command_ids["A"], command_ids["C"]},
        )

        tools.release["A"].set()
        await runtime.dispatcher.wait(command_ids["A"])
        self.assertIsNone(await runtime.advance(self.STREAM_ID))
        self.assertEqual(len(model.frames), 1)

        tools.release["C"].set()
        await runtime.dispatcher.wait(command_ids["C"])
        step_2 = await runtime.advance(self.STREAM_ID)
        self.assertIsNotNone(step_2)
        frame = model.frames[1]
        self.assertEqual(len(model.frames), 2)
        self.assertEqual(
            frame.state.command(command_ids["A"]).phase,
            CommandPhase.TERMINAL,
        )
        self.assertEqual(
            frame.state.command(command_ids["B"]).phase,
            CommandPhase.TERMINAL,
        )
        self.assertEqual(
            frame.state.command(command_ids["C"]).phase,
            CommandPhase.TERMINAL,
        )
        self.assertIn(
            frame.trigger_event.payload.command_id,
            set(command_ids.values()),
        )

    async def test_later_user_message_does_not_consume_outcome_trigger(self):
        tools = ControlledTools(("A", "B", "C"))
        model = ScriptedDecisionMaker((
            initial_tool_decision,
            lambda _frame: ModelDecision(content="observed tool outcomes"),
            lambda _frame: ModelDecision(content="follow user intent"),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            tools.handlers(),
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "collect",
            delivery_id="collect-1",
        )
        step_1 = await runtime.advance(self.STREAM_ID)
        command_ids = tool_command_ids(step_1)
        await asyncio.wait_for(tools.all_started.wait(), timeout=1)
        for name in tools.names:
            tools.release[name].set()
            await runtime.dispatcher.wait(command_ids[name])

        before = await runtime.state(self.STREAM_ID)
        self.assertEqual(before.status, RuntimeStatus.RUNNABLE)

        follow_up = await runtime.receive_user_message(
            self.STREAM_ID,
            "stop, summarize what you already read",
            delivery_id="collect-2",
        )
        step_2 = await runtime.advance(self.STREAM_ID)
        self.assertIsNotNone(step_2)
        outcome_frame = model.frames[1]

        self.assertEqual(len(model.frames), 2)
        self.assertIsInstance(
            outcome_frame.trigger_event.payload,
            CommandOutcomeReceived,
        )
        self.assertNotIn(
            follow_up.event_id,
            outcome_frame.state.visible_event_ids,
        )
        for command_id in command_ids.values():
            self.assertEqual(
                outcome_frame.state.command(command_id).phase,
                CommandPhase.TERMINAL,
            )

        step_3 = await runtime.advance(self.STREAM_ID)
        self.assertIsNotNone(step_3)
        user_frame = model.frames[2]
        self.assertEqual(len(model.frames), 3)
        self.assertEqual(step_3.trigger_event_id, follow_up.event_id)
        self.assertEqual(user_frame.trigger_event.event_id, follow_up.event_id)
        self.assertIsInstance(
            user_frame.trigger_event.payload,
            UserMessageReceived,
        )
        self.assertEqual(
            user_frame.trigger_event.payload.content,
            "stop, summarize what you already read",
        )
        events = await runtime._journal.snapshot(self.STREAM_ID)
        outcome_ids = {
            event.event_id for event in outcome_events(events)
        }
        self.assertTrue(outcome_ids <= set(user_frame.state.visible_event_ids))

    async def test_runtime_does_not_interpret_domain_ok_field(self):
        async def domain_rejection(_context, _arguments):
            return {
                "ok": False,
                "code": "DOMAIN_REJECTED",
                "error": "business rule rejected the request",
            }

        observed: list[tuple[OutcomeStatus, object]] = []

        def interpret_result(frame: DecisionFrame) -> ModelDecision:
            outcome = frame.state.commands[0].outcome
            observed.append((outcome.status, outcome.value))
            return ModelDecision(content="model handles the domain rejection")

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="try",
                    command_requests=(InvokeTool("domain_tool"),),
                ),
                interpret_result,
            )),
            {"domain_tool": ToolBinding(domain_rejection)},
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "try domain action",
            delivery_id="domain-1",
        )
        first = await runtime.advance(self.STREAM_ID)
        await runtime.dispatcher.wait(first.commands[0].command_id)
        second = await runtime.advance(self.STREAM_ID)
        state = await runtime.state(self.STREAM_ID)

        self.assertIsNotNone(second)
        self.assertEqual(observed[0][0], OutcomeStatus.SUCCEEDED)
        self.assertEqual(
            dict(observed[0][1]),
            {
                "ok": False,
                "code": "DOMAIN_REJECTED",
                "error": "business rule rejected the request",
            },
        )
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(len(state.steps), 2)

    async def test_abandon_cancel_and_late_outcomes_remain_distinct(self):
        scenario = await self._run_abandon_scenario()
        runtime = scenario.runtime
        command_ids = scenario.command_ids
        step_2 = scenario.step_2
        frame = scenario.decision_maker.frames[1]

        events = await scenario.journal.snapshot(self.STREAM_ID)
        a_outcome = next(
            event
            for event in outcome_events(events)
            if event.payload.command_id == command_ids["A"]
        )
        step_2_event = next(
            event
            for event in events
            if isinstance(event.payload, StepCommitted)
            and event.payload.step.step_id == step_2.step_id
        )
        self.assertLess(a_outcome.sequence, step_2_event.sequence)
        self.assertNotIn(a_outcome.event_id, frame.state.visible_event_ids)

        self.assertEqual(
            set(step_2.decision.abandon_command_ids),
            {command_ids["A"], command_ids["C"]},
        )
        cancel_targets = {
            command.effect.target_command_id: command.command_id
            for command in step_2.commands
            if isinstance(command.effect, CancelTool)
        }
        self.assertEqual(set(cancel_targets), {
            command_ids["A"],
            command_ids["C"],
        })

        state = await runtime.state(self.STREAM_ID)
        self.assertTrue(state.command(command_ids["A"]).abandoned)
        self.assertTrue(state.command(command_ids["C"]).abandoned)
        self.assertEqual(
            state.command(command_ids["A"]).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )
        self.assertEqual(
            state.command(command_ids["C"]).outcome.status,
            OutcomeStatus.CANCELLED,
        )
        self.assertEqual(
            state.command(cancel_targets[command_ids["A"]]).outcome.status,
            OutcomeStatus.NOT_APPLICABLE,
        )
        self.assertEqual(
            state.command(cancel_targets[command_ids["A"]]).outcome.value,
            "already_terminal",
        )
        self.assertEqual(
            state.command(cancel_targets[command_ids["C"]]).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )
        self.assertEqual(state.status, RuntimeStatus.RUNNABLE)
        self.assertEqual(len(scenario.decision_maker.frames), 2)
        cancel_outcome_step = await runtime.advance(self.STREAM_ID)
        self.assertIsNotNone(cancel_outcome_step)
        self.assertEqual(len(scenario.decision_maker.frames), 3)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.waiting_for, ("user_message",))
        self.assertTrue(scenario.tools.cancelled["C"].is_set())
        events = await scenario.journal.snapshot(self.STREAM_ID)
        c_outcome = next(
            event
            for event in outcome_events(events)
            if event.payload.command_id == command_ids["C"]
        )
        self.assertGreater(c_outcome.sequence, step_2_event.sequence)
        cancel_outcome_ids = {
            event.event_id
            for event in outcome_events(events)
            if event.payload.command_id in cancel_targets.values()
        }
        self.assertIn(
            cancel_outcome_step.trigger_event_id,
            cancel_outcome_ids,
        )

    async def test_decision_events_use_acceptance_order_and_frozen_views(self):
        tools = ControlledTools(("A", "B", "C"))
        joined_entered = asyncio.Event()
        release_joined = asyncio.Event()

        async def decide_joined(_frame: DecisionFrame) -> ModelDecision:
            joined_entered.set()
            await release_joined.wait()
            return ModelDecision(content="cohort handled")

        def decide_interrupt(_frame: DecisionFrame) -> ModelDecision:
            return ModelDecision(content="interrupt handled")

        model = ScriptedDecisionMaker((
            initial_tool_decision,
            decide_joined,
            decide_interrupt,
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            tools.handlers(),
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "collect",
            delivery_id="collect-1",
        )
        step_1 = await runtime.advance(self.STREAM_ID)
        command_ids = tool_command_ids(step_1)
        await asyncio.wait_for(tools.all_started.wait(), timeout=1)

        tools.release["B"].set()
        await runtime.dispatcher.wait(command_ids["B"])
        tools.release["A"].set()
        await runtime.dispatcher.wait(command_ids["A"])
        tools.release["C"].set()
        await runtime.dispatcher.wait(command_ids["C"])

        joined_task = asyncio.create_task(runtime.advance(self.STREAM_ID))
        await asyncio.wait_for(joined_entered.wait(), timeout=1)
        interrupt = await runtime.receive_interrupt(
            self.STREAM_ID,
            "change intent",
            delivery_id="interrupt-1",
        )
        release_joined.set()
        step_joined = await joined_task
        step_interrupt = await runtime.advance(self.STREAM_ID)

        frames = model.frames
        self.assertIsInstance(
            frames[1].trigger_event.payload,
            CommandOutcomeReceived,
        )
        self.assertIsInstance(
            frames[2].trigger_event.payload,
            UserInterruptReceived,
        )
        trigger_id = frames[1].trigger_event.payload.command_id
        self.assertIn(trigger_id, set(command_ids.values()))
        for command_id in command_ids.values():
            self.assertEqual(
                frames[1].state.command(command_id).phase,
                CommandPhase.TERMINAL,
            )

        events = await journal.snapshot(self.STREAM_ID)
        last_outcome = next(
            event
            for event in outcome_events(events)
            if event.payload.command_id == trigger_id
        )
        joined_event = next(
            event
            for event in events
            if isinstance(event.payload, StepCommitted)
            and event.payload.step.step_id == step_joined.step_id
        )
        self.assertLess(last_outcome.sequence, interrupt.sequence)
        self.assertLess(interrupt.sequence, joined_event.sequence)
        self.assertNotIn(interrupt.event_id, frames[1].state.visible_event_ids)
        self.assertIn(interrupt.event_id, frames[2].state.visible_event_ids)
        self.assertEqual(step_joined.trigger_event_id, last_outcome.event_id)
        self.assertEqual(step_interrupt.trigger_event_id, interrupt.event_id)
        self.assertEqual(len(model.frames), 3)

        turn = await runtime.turn(self.STREAM_ID)
        self.assertEqual(len(turn.interrupts), 1)
        self.assertEqual(turn.interrupts[0].event_id, interrupt.event_id)
        self.assertEqual(turn.interrupts[0].reason, "change intent")

    async def test_replay_rebuilds_views_without_model_or_tool_calls(self):
        scenario = await self._run_abandon_scenario()
        events = await scenario.journal.snapshot(self.STREAM_ID)
        live_state = await scenario.runtime.state(self.STREAM_ID)
        live_turn = await scenario.runtime.turn(self.STREAM_ID)
        live_trace = await scenario.runtime.trace(self.STREAM_ID)
        model_calls = len(scenario.decision_maker.frames)
        tool_calls = tuple(scenario.tools.executions)

        restored_journal = MemoryJournal(events)
        restored_events = await restored_journal.snapshot(self.STREAM_ID)
        restored = replay(self.STREAM_ID, restored_events)

        self.assertEqual(restored.state, live_state)
        self.assertEqual(restored.turn, live_turn)
        self.assertEqual(restored.trace, live_trace)
        self.assertEqual(restored_events, events)
        self.assertEqual(
            await scenario.journal.snapshot(self.STREAM_ID),
            events,
        )
        self.assertEqual(len(scenario.decision_maker.frames), model_calls)
        self.assertEqual(tuple(scenario.tools.executions), tool_calls)
        first_message = next(
            event
            for event in events
            if isinstance(event.payload, UserMessageReceived)
        )
        self.assertEqual(
            restored.turn.user_messages[0].event_id,
            first_message.event_id,
        )
        late_a = next(
            event
            for event in outcome_events(events)
            if event.payload.command_id == scenario.command_ids["A"]
        )
        self.assertIn(
            late_a.event_id,
            {entry.event_id for entry in restored.trace.entries},
        )

    async def test_cancel_uses_target_outcome_not_task_cancel_request(self):
        started = asyncio.Event()

        async def ignore_cancel(
            _context,
            _arguments: Mapping[str, object],
        ) -> object:
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return "completed-after-cancel"

        def cancel_a(frame: DecisionFrame) -> ModelDecision:
            command_id = frame.state.commands[0].command.command_id
            return ModelDecision(
                content="discard A",
                command_requests=(CancelTool(command_id),),
                abandon_command_ids=(command_id,),
            )

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="start A",
                command_requests=(InvokeTool("A"),),
            ),
            cancel_a,
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            {"A": ToolBinding(
                ignore_cancel,
                cancellation=(
                    CancellationContract.TASK_CANCELLED_ERROR_CONFIRMS
                ),
            )},
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="start-1",
        )
        first_step = await runtime.advance(self.STREAM_ID)
        target_id = first_step.commands[0].command_id
        await asyncio.wait_for(started.wait(), timeout=1)
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "stop A",
            delivery_id="interrupt-1",
        )
        cancel_step = await runtime.advance(self.STREAM_ID)
        cancel_id = cancel_step.commands[0].command_id
        await runtime.dispatcher.wait(cancel_id)

        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(
            state.command(target_id).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )
        self.assertEqual(
            state.command(target_id).outcome.value,
            "completed-after-cancel",
        )
        self.assertEqual(
            state.command(cancel_id).outcome.status,
            OutcomeStatus.NOT_APPLICABLE,
        )
        self.assertIn(
            state.command(cancel_id).outcome.value,
            ("already_terminal", "target_succeeded"),
        )

    async def test_cancel_during_outcome_commit_cannot_lose_terminal_fact(self):
        async def complete_immediately(
            _context,
            _arguments: Mapping[str, object],
        ) -> object:
            return "completed"

        def cancel_a(frame: DecisionFrame) -> ModelDecision:
            command_id = frame.state.commands[0].command.command_id
            return ModelDecision(
                content="discard A",
                command_requests=(CancelTool(command_id),),
                abandon_command_ids=(command_id,),
            )

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="start A",
                command_requests=(InvokeTool("A"),),
            ),
            cancel_a,
        ))
        journal = PausingOutcomeJournal()
        journal.pause_next_outcome = True
        runtime = AgentRuntime(
            journal,
            model,
            {"A": ToolBinding(
                complete_immediately,
                cancellation=(
                    CancellationContract.TASK_CANCELLED_ERROR_CONFIRMS
                ),
            )},
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="start-1",
        )
        first_step = await runtime.advance(self.STREAM_ID)
        target_id = first_step.commands[0].command_id
        await asyncio.wait_for(
            journal.outcome_append_started.wait(),
            timeout=1,
        )
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "stop A",
            delivery_id="interrupt-1",
        )
        cancel_step = await runtime.advance(self.STREAM_ID)
        cancel_id = cancel_step.commands[0].command_id
        journal.release_outcome_append.set()
        await runtime.dispatcher.wait(cancel_id)

        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(
            state.command(target_id).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )
        self.assertEqual(
            state.command(cancel_id).outcome.status,
            OutcomeStatus.NOT_APPLICABLE,
        )
        self.assertIn(
            state.command(cancel_id).outcome.value,
            ("already_terminal", "target_succeeded"),
        )

    async def test_wait_timeout_does_not_cancel_command(self):
        release = asyncio.Event()

        async def wait_for_release(
            _context,
            _arguments: Mapping[str, object],
        ) -> object:
            await release.wait()
            return "completed"

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="start A",
                command_requests=(InvokeTool("A"),),
            ),
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            {"A": ToolBinding(wait_for_release)},
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="start-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        command_id = step.commands[0].command_id

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                runtime.dispatcher.wait(command_id),
                timeout=0.01,
            )
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(
            state.command(command_id).phase,
            CommandPhase.UNKNOWN,
        )

        release.set()
        await runtime.dispatcher.wait(command_id)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(
            state.command(command_id).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )

    async def test_unsupported_cancel_never_claims_external_cancellation(self):
        external_effects: list[str] = []
        release = asyncio.Event()

        async def transfer(
            _context,
            _arguments: Mapping[str, object],
        ) -> object:
            external_effects.append("money-moved")
            await release.wait()
            return "receipt"

        def cancel_transfer(frame: DecisionFrame) -> ModelDecision:
            command_id = frame.state.commands[0].command.command_id
            return ModelDecision(
                content="stop waiting for transfer",
                command_requests=(CancelTool(command_id),),
                abandon_command_ids=(command_id,),
            )

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="transfer",
                command_requests=(InvokeTool("transfer"),),
            ),
            cancel_transfer,
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            {"transfer": ToolBinding(transfer)},
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "transfer",
            delivery_id="transfer-1",
        )
        first_step = await runtime.advance(self.STREAM_ID)
        target_id = first_step.commands[0].command_id
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "stop",
            delivery_id="interrupt-1",
        )
        cancel_step = await runtime.advance(self.STREAM_ID)
        cancel_id = cancel_step.commands[0].command_id
        await runtime.dispatcher.wait(cancel_id)

        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(external_effects, ["money-moved"])
        self.assertEqual(
            state.command(target_id).phase,
            CommandPhase.UNKNOWN,
        )
        self.assertEqual(
            state.command(cancel_id).outcome.status,
            OutcomeStatus.NOT_APPLICABLE,
        )
        self.assertEqual(
            state.command(cancel_id).outcome.value,
            "cancellation_unsupported",
        )

        release.set()
        await runtime.dispatcher.wait(target_id)
        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(
            state.command(target_id).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )

    async def test_pending_command_cancels_without_external_contract(self):
        async def never_invoked(
            _context,
            _arguments: Mapping[str, object],
        ) -> object:
            raise AssertionError("pending command must not be invoked")

        def cancel_pending(frame: DecisionFrame) -> ModelDecision:
            command_id = frame.state.commands[0].command.command_id
            return ModelDecision(
                content="cancel pending A",
                command_requests=(CancelTool(command_id),),
                abandon_command_ids=(command_id,),
            )

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="plan A",
                command_requests=(InvokeTool("A"),),
            ),
            cancel_pending,
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            {"A": ToolBinding(never_invoked)},
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "plan",
            delivery_id="plan-1",
        )
        initial_events = await journal.snapshot(self.STREAM_ID)
        initial_frame = runtime.projector.project(
            self.STREAM_ID,
            initial_events,
        ).next_decision
        request = StepClaimRequest(
            stream_id=self.STREAM_ID,
            trigger_event_id=initial_frame.trigger_event.event_id,
            decision_cursor=initial_frame.decision_cursor,
            basis_state_version=initial_frame.basis_state_version,
            observed_journal_position=initial_frame.observed_journal_position,
        )
        lease = await journal.acquire_step(
            request,
            token="manual-claim",
            owner_id="test",
            lease_seconds=30,
        )
        first_step_event = await runtime.step_runner.commit(
            initial_frame,
            lease,
        )
        target_id = first_step_event.payload.step.commands[0].command_id
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "stop",
            delivery_id="interrupt-1",
        )
        cancel_step = await runtime.advance(self.STREAM_ID)
        cancel_id = cancel_step.commands[0].command_id
        await runtime.dispatcher.wait(cancel_id)

        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(
            state.command(target_id).outcome.status,
            OutcomeStatus.CANCELLED,
        )
        self.assertEqual(
            state.command(cancel_id).outcome.status,
            OutcomeStatus.SUCCEEDED,
        )

    async def test_event_payload_is_bounded(self):
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            ScriptedDecisionMaker(()),
            {},
            SequentialIds(),
        )

        with self.assertRaisesRegex(ValueError, "maximum size"):
            await runtime.receive_user_message(
                self.STREAM_ID,
                "x" * (300 * 1024),
                delivery_id="oversized-1",
            )
        self.assertEqual(await journal.snapshot(self.STREAM_ID), ())

    def test_step_commands_must_match_decision_requests(self):
        decision = ModelDecision(
            content="invoke A",
            command_requests=(InvokeTool("A"),),
        )
        with self.assertRaisesRegex(ValueError, "do not match"):
            Step(
                step_id="step-1",
                trigger_event_id="event-1",
                decision_cursor=1,
                basis_state_version="version-1",
                observed_journal_position=1,
                decision=decision,
                commands=(Command("command-1", InvokeTool("B")),),
            )

    def test_restored_event_uses_the_same_envelope_validation(self):
        with self.assertRaisesRegex(ValueError, "UTC"):
            Event(
                event_id="event-1",
                stream_id=self.STREAM_ID,
                sequence=1,
                payload=UserMessageReceived("hello"),
                occurred_at=datetime.now(),
                causation_id=None,
                correlation_id=None,
                schema_version=1,
                artifact_refs=(),
            )

    async def test_replay_rejects_false_command_causation(self):
        scenario = await self._run_abandon_scenario()
        events = await scenario.journal.snapshot(self.STREAM_ID)
        dispatch_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event.payload, DispatchAttemptStarted)
        )
        false_dispatch = list(events)
        false_dispatch[dispatch_index] = replace(
            false_dispatch[dispatch_index],
            causation_id="bogus",
        )
        with self.assertRaisesRegex(ValueError, "dispatch causation"):
            replay(self.STREAM_ID, tuple(false_dispatch))

        outcome_index = next(
            index
            for index, event in enumerate(events)
            if (
                isinstance(event.payload, CommandOutcomeReceived)
                and event.payload.attempt_id is not None
            )
        )
        false_outcome = list(events)
        false_outcome[outcome_index] = replace(
            false_outcome[outcome_index],
            causation_id="bogus",
        )
        with self.assertRaisesRegex(ValueError, "causation mismatch"):
            replay(self.STREAM_ID, tuple(false_outcome))

    async def test_replay_rejects_false_observed_position_and_schema(self):
        scenario = await self._run_abandon_scenario()
        events = await scenario.journal.snapshot(self.STREAM_ID)
        step_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(event.payload, StepCommitted)
        )
        false_position = list(events)
        step_event = false_position[step_index]
        false_position[step_index] = replace(
            step_event,
            payload=StepCommitted(replace(
                step_event.payload.step,
                observed_journal_position=step_event.sequence,
            )),
        )
        with self.assertRaisesRegex(ValueError, "observed position"):
            replay(self.STREAM_ID, tuple(false_position))

        step_events = [
            event
            for event in events
            if isinstance(event.payload, StepCommitted)
        ]
        duplicate_step_id = list(events)
        second_step_index = events.index(step_events[1])
        duplicate_step_id[second_step_index] = replace(
            step_events[1],
            payload=StepCommitted(replace(
                step_events[1].payload.step,
                step_id=step_events[0].payload.step.step_id,
            )),
        )
        with self.assertRaisesRegex(ValueError, "duplicate step id"):
            replay(self.STREAM_ID, tuple(duplicate_step_id))

        journal = MemoryJournal()
        with self.assertRaisesRegex(ValueError, "schema version"):
            await journal.accept_delivery(EventDraft(
                event_id="future-event",
                stream_id=self.STREAM_ID,
                payload=UserMessageReceived("hello"),
                occurred_at=datetime.now(timezone.utc),
                schema_version=EVENT_SCHEMA_VERSION + 1,
                delivery=DeliveryIdentity("user", "future-event"),
            ))

    async def test_unrecordable_tool_result_remains_unknown(self):
        async def oversized_result(
            _context,
            _arguments: Mapping[str, object],
        ) -> object:
            return "x" * (200 * 1024)

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="start A",
                command_requests=(InvokeTool("A"),),
            ),
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            {"A": ToolBinding(oversized_result)},
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="start-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        command_id = step.commands[0].command_id
        with self.assertRaisesRegex(ValueError, "maximum size"):
            await runtime.dispatcher.wait(command_id)

        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(
            state.command(command_id).phase,
            CommandPhase.UNKNOWN,
        )
        self.assertIsNone(state.command(command_id).outcome)

    async def test_raw_tool_error_is_reraised_and_remains_unknown(self):
        message = "x" * (300 * 1024)

        async def oversized_error(
            _context,
            _arguments: Mapping[str, object],
        ) -> object:
            raise RuntimeError(message)

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="start A",
                command_requests=(InvokeTool("A"),),
            ),
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            {"A": ToolBinding(oversized_error)},
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="start-1",
        )
        step = await runtime.advance(self.STREAM_ID)
        command_id = step.commands[0].command_id
        with self.assertRaises(RuntimeError) as caught:
            await runtime.dispatcher.wait(command_id)
        self.assertEqual(str(caught.exception), message)

        state = await runtime.state(self.STREAM_ID)
        self.assertEqual(
            state.command(command_id).phase,
            CommandPhase.UNKNOWN,
        )
        self.assertIsNone(state.command(command_id).outcome)

    async def _run_abandon_scenario(self) -> AbandonScenario:
        tools = ControlledTools(("A", "B", "C"))
        step_2_entered = asyncio.Event()
        release_step_2 = asyncio.Event()

        async def abandon_and_cancel(frame: DecisionFrame) -> ModelDecision:
            step_2_entered.set()
            await release_step_2.wait()
            ids = {
                state.command.effect.name: state.command.command_id
                for state in frame.state.commands
                if isinstance(state.command.effect, InvokeTool)
            }
            return ModelDecision(
                content="B is enough; discard A and C",
                command_requests=(
                    CancelTool(ids["A"]),
                    CancelTool(ids["C"]),
                ),
                abandon_command_ids=(ids["A"], ids["C"]),
            )

        model = ScriptedDecisionMaker((
            initial_tool_decision,
            abandon_and_cancel,
            lambda _frame: ModelDecision(content="cancel outcomes observed"),
        ))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            model,
            tools.handlers(),
            SequentialIds(),
        )

        await runtime.receive_user_message(
            self.STREAM_ID,
            "collect",
            delivery_id="collect-1",
        )
        step_1 = await runtime.advance(self.STREAM_ID)
        command_ids = tool_command_ids(step_1)
        await asyncio.wait_for(tools.all_started.wait(), timeout=1)

        tools.release["B"].set()
        await runtime.dispatcher.wait(command_ids["B"])
        await runtime.receive_interrupt(
            self.STREAM_ID,
            "keep B only",
            delivery_id="interrupt-1",
        )
        step_2_task = asyncio.create_task(runtime.advance(self.STREAM_ID))
        await asyncio.wait_for(step_2_entered.wait(), timeout=1)

        tools.release["A"].set()
        await runtime.dispatcher.wait(command_ids["A"])
        release_step_2.set()
        step_2 = await step_2_task
        for command in step_2.commands:
            await runtime.dispatcher.wait(command.command_id)

        return AbandonScenario(
            runtime=runtime,
            journal=journal,
            decision_maker=model,
            tools=tools,
            command_ids=command_ids,
            step_2=step_2,
        )


if __name__ == "__main__":
    unittest.main()
