from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Awaitable, Callable

from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.artifacts import MemoryArtifactGateway
from helperme.assistant.completion.criteria import (
    CriteriaCommitted,
    CriteriaSource,
    criteria_fact,
)
from helperme.assistant.context.projection import (
    ModelContextProjector,
    project_chat_messages,
)
from helperme.assistant.toolsets import ToolSurface
from helperme.assistant.decision import (
    JournalBackedLlmDecisionMaker,
    decision_from_llm,
)
from helperme.assistant.runner import (
    StreamNotFoundError,
    drive_until_idle,
    pending_authorization_ids,
    resume_stream,
)
from helperme.assistant.streams import AssistantStreams
from helperme.channels.cli.console import drive_with_console_interrupts
from helperme.runtime import (
    AgentRuntime,
    CommandPhase,
    CommandRecoveryRequired,
    InvokeTool,
    LifecycleIntent,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    StepCommitted,
    ToolBinding,
    UserInterruptReceived,
    UserMessageReceived,
)
from helperme.runtime.state import DecisionFrame
from helperme.llm.types import (
    InvalidLLMResponse,
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    ToolCall,
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


class PausingDecisionSnapshotJournal(MemoryJournal):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_count = 0
        self.decision_snapshot_started = asyncio.Event()
        self.release_decision_snapshot = asyncio.Event()

    async def snapshot(self, stream_id: str):
        self.snapshot_count += 1
        if self.snapshot_count == 2:
            self.decision_snapshot_started.set()
            await self.release_decision_snapshot.wait()
        return await super().snapshot(stream_id)


class RecordingLlm:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.tools: list[dict[str, object]] = []

    async def chat(self, messages, _model, *, tools=None):
        self.messages = messages
        self.tools = tools or []
        return LLMCallResult(
            LLMResponse(content="done"),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class AssistantRunnerTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "harness-stream"

    def test_decision_from_llm_maps_text_and_tool_calls(self):
        decision = decision_from_llm(
            LLMResponse(
                content="looking",
                calls=(
                    ToolCall("call-1", "read_file", '{"path": "a.py"}'),
                ),
            ),
            frozenset({"read_file"}),
        )
        self.assertEqual(decision.content, "looking")
        self.assertEqual(
            decision.command_requests,
            (InvokeTool("read_file", (("path", "a.py"),)),),
        )

    def test_decision_from_llm_rejects_model_deliver(self):
        with self.assertRaisesRegex(ValueError, "product command"):
            decision_from_llm(
                LLMResponse(
                    content="hi",
                    calls=(
                        ToolCall(
                            "call-1",
                            DELIVER_TOOL_NAME,
                            '{"text": "hi"}',
                        ),
                    ),
                ),
                frozenset(),
            )

    def test_decision_from_llm_rejects_unoffered_tool(self):
        with self.assertRaisesRegex(
            InvalidLLMResponse,
            "was not offered",
        ):
            decision_from_llm(
                LLMResponse(
                    calls=(ToolCall("call-1", "invented", "{}"),),
                ),
                frozenset({"read_file"}),
            )

    async def test_decision_context_ignores_facts_after_observed_position(self):
        journal = PausingDecisionSnapshotJournal()
        llm = RecordingLlm()
        gateway = MemoryArtifactGateway()
        projector = ModelContextProjector(gateway=gateway)
        schemas = [{
            "type": "function",
            "function": {
                "name": "stable_tool",
                "description": "stable",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        runtime = AgentRuntime(
            journal,
            JournalBackedLlmDecisionMaker(
                journal,
                llm,
                "test-model",
                tool_schemas=schemas,
                system_prompt="frozen prompt",
                projector=projector,
            ),
            deliver_binding(lambda _text: None),
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "start",
            delivery_id="ask-1",
        )

        advancing = asyncio.create_task(runtime.advance(self.STREAM_ID))
        await asyncio.wait_for(
            journal.decision_snapshot_started.wait(),
            timeout=1,
        )
        await runtime.record_fact(
            self.STREAM_ID,
            criteria_fact(CriteriaCommitted(
                version=1,
                user_objective="late objective",
                strict_completion=True,
                inferred=(),
                source=CriteriaSource.USER,
            )),
        )
        schemas[0]["function"]["name"] = "mutated_tool"
        journal.release_decision_snapshot.set()
        await advancing

        events = await journal.snapshot(self.STREAM_ID)
        committed = next(
            event
            for event in events
            if isinstance(event.payload, StepCommitted)
        )
        self.assertEqual(len(committed.artifact_refs), 1)
        manifest_ref = committed.artifact_refs[0]
        manifest = json.loads(
            gateway.for_stream(self.STREAM_ID).contents[manifest_ref]
        )

        self.assertEqual(llm.messages[0]["content"], "frozen prompt")
        self.assertNotIn("late objective", llm.messages[0]["content"])
        self.assertEqual(
            llm.tools[0]["function"]["name"],
            "stable_tool",
        )
        self.assertEqual(
            manifest["request"]["messages"][0]["content"],
            "frozen prompt",
        )
        self.assertEqual(
            manifest["request"]["tools"][0]["function"]["name"],
            "stable_tool",
        )
        self.assertEqual(manifest["request"]["model"], "test-model")
        self.assertEqual(manifest["response"]["content"], "done")

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
        )
        events = await runtime._journal.snapshot(self.STREAM_ID)
        messages = project_chat_messages(
            events,
            tuple(event.event_id for event in events),
            "sys",
        )

        self.assertEqual(delivered, ["hello"])
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(result.state.waiting_for, ("user_message",))
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant"],
        )
        self.assertEqual(messages[2]["content"], "hello")
        self.assertNotIn("tool_calls", messages[2])

    async def test_drive_continues_until_semantic_idle(self):
        tool_steps = 12
        decisions = tuple(
            lambda _frame: ModelDecision(
                content="working",
                command_requests=(InvokeTool("ping"),),
            )
            for _ in range(tool_steps)
        ) + (
            lambda _frame: ModelDecision(
                content="done",
                command_requests=(
                    InvokeTool(DELIVER_TOOL_NAME, (("text", "done"),)),
                ),
            ),
        )

        async def ping(_context, _arguments):
            return {"ok": True}

        delivered: list[str] = []
        model = ScriptedDecisionMaker(decisions)
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {
                "ping": ToolBinding(ping),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "keep going",
            delivery_id="continuous-1",
        )

        result = await drive_until_idle(runtime, self.STREAM_ID)

        self.assertEqual(len(model.frames), tool_steps + 1)
        self.assertEqual(delivered, ["done"])
        self.assertEqual(result.state.waiting_for, ("user_message",))

    async def test_resume_stream_uses_explicit_existing_identity(self):
        surface = ToolSurface()
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(content="waiting"),
            )),
            {},
            SequentialIds(),
        )
        surface.attach(runtime)
        await runtime.receive_user_message(
            self.STREAM_ID,
            "hello",
            delivery_id="resume-1",
        )
        await runtime.advance(self.STREAM_ID)

        state = await resume_stream(runtime, surface, self.STREAM_ID)

        self.assertEqual(state.stream_id, self.STREAM_ID)
        self.assertEqual(state.status, RuntimeStatus.WAITING)

    async def test_resume_stream_rejects_unknown_identity(self):
        surface = ToolSurface()
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(()),
            {},
            SequentialIds(),
        )
        surface.attach(runtime)

        with self.assertRaises(StreamNotFoundError):
            await resume_stream(runtime, surface, "missing-stream")

    async def test_resume_stream_accepts_created_empty_identity(self):
        surface = ToolSurface()
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(()),
            {},
            SequentialIds(),
        )
        surface.attach(runtime)
        self.assertTrue(await runtime.create_stream("empty-stream"))

        state = await resume_stream(runtime, surface, "empty-stream")

        self.assertEqual(state.stream_id, "empty-stream")
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(await runtime.snapshot("empty-stream"), ())

    async def test_stream_service_hides_runtime_state_from_channel(self):
        surface = ToolSurface()
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(content="handled"),
            )),
            {},
            SequentialIds(),
        )
        surface.attach(runtime)
        streams = AssistantStreams(runtime, surface)

        created = await streams.create(self.STREAM_ID)
        await streams.receive_user_message(
            self.STREAM_ID,
            "work",
            delivery_id="message-1",
        )
        finished = await streams.drive(self.STREAM_ID)

        self.assertEqual(created.status, RuntimeStatus.WAITING.value)
        self.assertEqual(finished.status, RuntimeStatus.WAITING.value)
        self.assertFalse(finished.terminal)
        with self.assertRaisesRegex(ValueError, "Stream 已存在"):
            await streams.create(self.STREAM_ID)

    async def test_drive_exposes_internal_error_and_leaves_attempt_unknown(self):
        async def broken(_context, _arguments):
            raise RuntimeError("internal bug")

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="call broken tool",
                    command_requests=(InvokeTool("broken"),),
                ),
            )),
            {"broken": ToolBinding(broken)},
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "run",
            delivery_id="broken-1",
        )

        with self.assertRaisesRegex(RuntimeError, "internal bug"):
            await drive_until_idle(runtime, self.STREAM_ID)

        state = await runtime.state(self.STREAM_ID)
        events = await runtime.snapshot(self.STREAM_ID)
        self.assertIs(state.commands[0].phase, CommandPhase.UNKNOWN)
        self.assertFalse(any(
            isinstance(event.payload, CommandRecoveryRequired)
            for event in events
        ))

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
        )
        self.assertEqual(delivered, ["finished"])
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
        )
        events = await runtime._journal.snapshot(self.STREAM_ID)
        messages = project_chat_messages(
            events,
            tuple(event.event_id for event in events),
            "sys",
        )
        roles = [message["role"] for message in messages]

        self.assertEqual(delivered, ["checking", "done"])
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

    async def test_concurrent_cli_text_becomes_interrupt_before_tools_finish(self):
        started = asyncio.Event()
        release = asyncio.Event()
        interrupt_decision_started = asyncio.Event()

        async def slow(_context, _arguments):
            started.set()
            await release.wait()
            return "slow-result"

        def handle_interrupt(frame):
            interrupt_decision_started.set()
            return ModelDecision(content="interrupt handled")

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="",
                command_requests=(InvokeTool("slow"),),
            ),
            handle_interrupt,
            lambda _frame: ModelDecision(content="tool group handled"),
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
        streams = AssistantStreams(
            runtime,
            ToolSurface(),
        )
        input_queue: asyncio.Queue[str | None] = asyncio.Queue()
        drive = asyncio.create_task(drive_with_console_interrupts(
            streams,
            self.STREAM_ID,
            input_queue,
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        await input_queue.put("只保留已经取得的结果")
        await asyncio.wait_for(interrupt_decision_started.wait(), timeout=1)
        self.assertFalse(release.is_set())
        self.assertIsInstance(
            model.frames[1].trigger_event.payload,
            UserInterruptReceived,
        )
        self.assertEqual(
            model.frames[1].trigger_event.payload.reason,
            "只保留已经取得的结果",
        )
        release.set()
        result = await drive
        self.assertEqual(result.status, RuntimeStatus.WAITING.value)
        self.assertEqual(len(model.frames), 3)

    async def test_drive_yields_for_authorization_then_resumes_on_grant(self):
        executions: list[object] = []
        delivered: list[str] = []

        async def secret(_context, arguments):
            executions.append(dict(arguments))
            return {"ok": True, "code": "OK", "data": arguments}

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="need auth",
                    command_requests=(InvokeTool("secret"),),
                ),
                lambda _frame: ModelDecision(
                    content="done",
                    command_requests=(
                        InvokeTool(
                            DELIVER_TOOL_NAME,
                            (("text", "authorized"),),
                        ),
                    ),
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
            {
                "secret": ToolBinding(
                    secret,
                    requires_authorization=True,
                ),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "do the secret thing",
            delivery_id="ask-auth",
        )
        waiting = await drive_until_idle(
            runtime,
            self.STREAM_ID,
        )
        command_ids = pending_authorization_ids(waiting.state)

        self.assertEqual(waiting.state.status, RuntimeStatus.WAITING)
        self.assertEqual(len(command_ids), 1)
        self.assertEqual(executions, [])

        granted = await runtime.grant_command(self.STREAM_ID, command_ids[0])
        self.assertIsNotNone(granted)
        finished = await drive_until_idle(
            runtime,
            self.STREAM_ID,
        )

        self.assertEqual(executions, [{}])
        self.assertEqual(delivered, ["authorized"])
        self.assertEqual(finished.state.status, RuntimeStatus.COMPLETED)

    async def test_user_message_during_authorization_opens_a_step(self):
        executions: list[object] = []
        delivered: list[str] = []

        async def secret(_context, arguments):
            executions.append(dict(arguments))
            return {"ok": True, "code": "OK", "data": arguments}

        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="need auth",
                command_requests=(InvokeTool("secret"),),
            ),
            lambda _frame: ModelDecision(
                content="heard continue",
                command_requests=(
                    InvokeTool(
                        DELIVER_TOOL_NAME,
                        (("text", "heard continue"),),
                    ),
                ),
            ),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {
                "secret": ToolBinding(
                    secret,
                    requires_authorization=True,
                ),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "do the secret thing",
            delivery_id="ask-auth",
        )
        waiting = await drive_until_idle(
            runtime,
            self.STREAM_ID,
        )
        command_ids = pending_authorization_ids(waiting.state)
        await runtime.receive_user_message(
            self.STREAM_ID,
            "继续",
            delivery_id="say-continue",
        )
        after = await drive_until_idle(
            runtime,
            self.STREAM_ID,
        )

        self.assertEqual(len(model.frames), 2)
        self.assertIsInstance(
            model.frames[1].trigger_event.payload,
            UserMessageReceived,
        )
        self.assertEqual(model.frames[1].trigger_event.payload.content, "继续")
        self.assertEqual(executions, [])
        self.assertEqual(delivered, ["heard continue"])
        self.assertEqual(after.state.status, RuntimeStatus.WAITING)
        self.assertEqual(pending_authorization_ids(after.state), command_ids)
