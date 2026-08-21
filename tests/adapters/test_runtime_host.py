from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable, Callable

from adapters.delivery import DELIVER_TOOL_NAME, deliver_binding
from adapters.runtime_host import (
    InterruptFlag,
    decision_from_llm,
    drive_until_idle,
    project_chat_messages,
)
from agent_runtime import (
    AgentRuntime,
    CommandPhase,
    InvokeTool,
    LifecycleIntent,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    ToolBinding,
    UserMessageReceived,
)
from agent_runtime.state import DecisionFrame
from core.model_call.types import LLMResponse, ToolCall


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


class RuntimeHostTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "harness-stream"

    def test_decision_from_llm_maps_text_and_tool_calls(self):
        decision = decision_from_llm(LLMResponse(
            content="looking",
            calls=(
                ToolCall("call-1", "read_file", '{"path": "a.py"}'),
            ),
        ))
        self.assertEqual(decision.content, "looking")
        self.assertEqual(
            decision.command_requests,
            (InvokeTool("read_file", (("path", "a.py"),)),),
        )

    def test_decision_from_llm_rejects_model_deliver(self):
        with self.assertRaisesRegex(ValueError, "product command"):
            decision_from_llm(LLMResponse(
                content="hi",
                calls=(ToolCall("call-1", DELIVER_TOOL_NAME, '{"text": "hi"}'),),
            ))

    async def test_drive_delivers_then_waits_for_user(self):
        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="hello",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "hello"),)),
                    ),
                ),
            )),
            deliver_binding(delivered.append),
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "hi",
            delivery_id="ask-1",
        )
        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
        )
        events = await runtime._journal.snapshot(self.STREAM_ID)
        messages = project_chat_messages(
            events,
            tuple(event.event_id for event in events),
            "sys",
        )

        self.assertEqual(delivered, ["hello"])
        self.assertFalse(result.paused)
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(result.state.waiting_for, ("user_message",))
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant"],
        )
        self.assertEqual(messages[2]["content"], "hello")
        self.assertNotIn("tool_calls", messages[2])

    async def test_drive_finalizes_complete_after_deliver(self):
        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="finished",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "finished"),)),
                    ),
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
            deliver_binding(delivered.append),
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "wrap up",
            delivery_id="ask-1",
        )
        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
        )
        self.assertEqual(delivered, ["finished"])
        self.assertFalse(result.paused)
        self.assertEqual(result.state.status, RuntimeStatus.COMPLETED)

    async def test_tool_outcome_is_visible_but_deliver_is_not(self):
        delivered: list[str] = []

        async def ping(_context, _arguments):
            return "pong"

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="checking",
                    command_requests=(
                        InvokeTool("ping"),
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "checking"),)),
                    ),
                ),
                lambda _frame: ModelDecision(
                    content="done",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "done"),)),
                    ),
                ),
            )),
            {
                "ping": ToolBinding(ping),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "go",
            delivery_id="ask-1",
        )
        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
        )
        events = await runtime._journal.snapshot(self.STREAM_ID)
        messages = project_chat_messages(
            events,
            tuple(event.event_id for event in events),
            "sys",
        )
        roles = [message["role"] for message in messages]

        self.assertEqual(delivered, ["checking", "done"])
        self.assertFalse(result.paused)
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(len(messages[2]["tool_calls"]), 1)
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["name"], "ping")
        self.assertIn("pong", messages[3]["content"])

    async def test_parallel_tool_followup_sees_the_whole_cohort(self):
        delivered: list[str] = []

        def handler(name: str):
            async def _handler(_context, _arguments):
                return name
            return _handler

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="reading",
                command_requests=(
                    InvokeTool("read_a"),
                    InvokeTool("read_b"),
                    InvokeTool("read_c"),
                ),
            ),
            lambda _frame: ModelDecision(
                content="saw all three",
                command_requests=(
                    InvokeTool(
                        DELIVER_TOOL_NAME,
                        (("text", "saw all three"),),
                    ),
                ),
            ),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {
                "read_a": ToolBinding(handler("a")),
                "read_b": ToolBinding(handler("b")),
                "read_c": ToolBinding(handler("c")),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "read them",
            delivery_id="ask-1",
        )
        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
        )
        events = await runtime._journal.snapshot(self.STREAM_ID)
        second_frame = model.frames[1]
        messages = project_chat_messages(
            events,
            second_frame.state.visible_event_ids,
            "sys",
        )
        tool_messages = [
            message for message in messages if message["role"] == "tool"
        ]
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(delivered, ["saw all three"])
        self.assertEqual(len(tool_messages), 3)

    async def test_interrupt_stops_the_pump_without_a_new_step(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(_context, _arguments):
            started.set()
            await release.wait()
            return "slow-result"

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="",
                command_requests=(InvokeTool("slow"),),
            ),
            lambda _frame: ModelDecision(content="should not run"),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {"slow": ToolBinding(slow)},
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="ask-1",
        )
        interrupt = InterruptFlag()
        drive = asyncio.create_task(drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
            interrupt_requested=interrupt,
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        interrupt.set()
        release.set()
        result = await drive
        self.assertTrue(result.paused)
        self.assertEqual(len(model.frames), 1)

    async def test_paused_continue_is_triggered_by_the_new_user_message(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(_context, _arguments):
            started.set()
            await release.wait()
            return "notes"

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="reading",
                command_requests=(
                    InvokeTool("read_a"),
                    InvokeTool("read_b"),
                ),
            ),
            lambda frame: ModelDecision(
                content="follow user intent",
                command_requests=(
                    InvokeTool(
                        DELIVER_TOOL_NAME,
                        (("text", "follow user intent"),),
                    ),
                ),
            ),
        ))
        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {
                "read_a": ToolBinding(slow),
                "read_b": ToolBinding(slow),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "read files",
            delivery_id="ask-1",
        )
        interrupt = InterruptFlag()
        drive = asyncio.create_task(drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
            interrupt_requested=interrupt,
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        interrupt.set()
        release.set()
        paused = await drive
        self.assertTrue(paused.paused)
        self.assertEqual(len(model.frames), 1)

        await runtime.receive_user_message(
            self.STREAM_ID,
            "just list what you already found",
            delivery_id="ask-2",
        )
        continued = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
        )
        self.assertFalse(continued.paused)
        self.assertEqual(len(model.frames), 2)
        self.assertIsInstance(
            model.frames[1].trigger_event.payload,
            UserMessageReceived,
        )
        self.assertEqual(
            model.frames[1].trigger_event.payload.content,
            "just list what you already found",
        )
        self.assertEqual(
            {
                state.command.effect.name: state.phase
                for state in model.frames[1].state.commands
                if isinstance(state.command.effect, InvokeTool)
                and state.command.effect.name != DELIVER_TOOL_NAME
            },
            {
                "read_a": CommandPhase.TERMINAL,
                "read_b": CommandPhase.TERMINAL,
            },
        )
        self.assertEqual(delivered, ["follow user intent"])


class ConsoleEngineArgTest(unittest.TestCase):
    def test_engine_defaults_to_core(self):
        from console_chat import _parse_args

        self.assertEqual(_parse_args([]).engine, "core")

    def test_engine_runtime_is_accepted(self):
        from console_chat import _parse_args

        self.assertEqual(_parse_args(["--engine", "runtime"]).engine, "runtime")
