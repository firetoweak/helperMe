from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable, Mapping

from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from tests.session_scheduler import settle_session
from helperme.assistant.toolsets import (
    LOAD_TOOLSET,
    LoadedTool,
    ToolSurface,
    ToolsetDescriptor,
    ToolsetLoadError,
    load_toolset_binding,
)
from helperme.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    StepCommitted,
)
from helperme.runtime.state import DecisionFrame


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


class FakeEchoProvider:
    def __init__(
        self,
        revision: int = 1,
        *,
        requires_authorization: bool = False,
    ) -> None:
        self.revision = revision
        self.requires_authorization = requires_authorization

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        return (ToolsetDescriptor("demo", "echo tools", self.revision),)

    async def load(self, toolset_id: str) -> tuple[LoadedTool, ...]:
        if toolset_id != "demo":
            raise ToolsetLoadError(
                "TOOLSET_NOT_FOUND",
                f"Toolset {toolset_id} not found",
            )

        async def ping(arguments: Mapping[str, object]) -> object:
            return {"ok": True, "data": {"echo": arguments.get("text", "")}}

        return (
            LoadedTool(
                name="demo_ping",
                description="echo text",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
                execute=ping,
                requires_authorization=self.requires_authorization,
            ),
        )


def _schema_names(schemas: list[dict[str, object]]) -> set[str]:
    names: set[str] = set()
    for schema in schemas:
        function = schema.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


class ToolsetProgressiveLoadTest(unittest.IsolatedAsyncioTestCase):
    SESSION_ID = "toolset-session"

    async def _committed_load_events(self):
        delivered: list[str] = []
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        decisions = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    content="loading",
                    command_requests=(
                        InvokeTool(LOAD_TOOLSET, (("toolset_id", "demo"),)),
                    ),
                ),
                lambda _frame: ModelDecision(
                    content="done",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "done"),)),
                    ),
                ),
            )
        )
        runtime = AgentRuntime(
            MemoryJournal(),
            decisions,
            {
                **load_toolset_binding(surface),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        surface.attach(runtime)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "load echo",
            delivery_id="load-1",
        )
        await settle_session(runtime, self.SESSION_ID)
        return await runtime.snapshot(self.SESSION_ID)

    def test_catalog_does_not_expose_loaded_tools_before_load(self):
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        names = _schema_names(surface.schemas(self.SESSION_ID))
        self.assertEqual(names, {LOAD_TOOLSET})
        self.assertIn("demo", surface.catalog_instruction(self.SESSION_ID))
        self.assertNotIn("demo_ping", surface.catalog_instruction(self.SESSION_ID))

    async def test_unknown_toolset_is_a_model_correctable_error(self):
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(()),
            load_toolset_binding(surface),
            SequentialIds(),
        )
        surface.attach(runtime)
        result = await surface.load(self.SESSION_ID, "missing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TOOLSET_NOT_FOUND")
        self.assertEqual(
            _schema_names(surface.schemas(self.SESSION_ID)),
            {LOAD_TOOLSET},
        )

    async def test_load_toolset_makes_tools_visible_on_the_next_step(self):
        delivered: list[str] = []
        surface = ToolSurface(
            providers=(FakeEchoProvider(),),
            reserved_names=(DELIVER_TOOL_NAME,),
        )
        seen: list[set[str]] = []

        def first(_frame):
            seen.append(_schema_names(surface.schemas(self.SESSION_ID)))
            return ModelDecision(
                content="loading",
                command_requests=(InvokeTool(LOAD_TOOLSET, (("toolset_id", "demo"),)),),
            )

        def second(_frame):
            seen.append(_schema_names(surface.schemas(self.SESSION_ID)))
            return ModelDecision(
                content="pinging",
                command_requests=(InvokeTool("demo_ping", (("text", "hi"),)),),
            )

        def third(_frame):
            return ModelDecision(
                content="done",
                command_requests=(InvokeTool(DELIVER_TOOL_NAME, (("text", "done"),)),),
            )

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((first, second, third)),
            {
                **load_toolset_binding(surface),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        surface.attach(runtime)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "use echo",
            delivery_id="ask-1",
        )
        result = await settle_session(
            runtime,
            self.SESSION_ID,
        )
        events = await runtime.snapshot(self.SESSION_ID)
        names: dict[str, str] = {}
        for event in events:
            payload = event.payload
            if isinstance(payload, StepCommitted):
                for command in payload.step.commands:
                    effect = command.effect
                    if isinstance(effect, InvokeTool):
                        names[command.command_id] = effect.name
        echoes = [
            event.payload.outcome.value
            for event in events
            if isinstance(event.payload, CommandOutcomeReceived)
            and names.get(event.payload.command_id) == "demo_ping"
        ]

        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(delivered, ["done"])
        self.assertEqual(seen[0], {LOAD_TOOLSET})
        self.assertEqual(seen[1], {LOAD_TOOLSET, "demo_ping"})
        self.assertEqual(echoes[0]["data"]["echo"], "hi")

    async def test_loaded_toolset_cache_can_be_rehydrated_from_journal(self):
        events = await self._committed_load_events()
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(()),
            load_toolset_binding(surface),
            SequentialIds(),
        )
        surface.attach(runtime)

        activations = await surface.rehydrate(self.SESSION_ID, events)

        self.assertEqual(
            [(item.toolset_id, item.revision) for item in activations],
            [("demo", 1)],
        )
        self.assertEqual(
            _schema_names(surface.schemas(self.SESSION_ID)),
            {LOAD_TOOLSET, "demo_ping"},
        )

    async def test_failed_load_outcome_does_not_break_rehydrate(self):
        delivered: list[str] = []
        surface = ToolSurface(providers=(FakeEchoProvider(),))
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(
                (
                    lambda _frame: ModelDecision(
                        content="missing",
                        command_requests=(
                            InvokeTool(
                                LOAD_TOOLSET,
                                (("toolset_id", "missing"),),
                            ),
                        ),
                    ),
                    lambda _frame: ModelDecision(
                        command_requests=(
                            InvokeTool(
                                DELIVER_TOOL_NAME,
                                (("text", "done"),),
                            ),
                        ),
                    ),
                )
            ),
            {
                **load_toolset_binding(surface),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        surface.attach(runtime)
        await runtime.receive_user_message(
            self.SESSION_ID,
            "load missing",
            delivery_id="missing-1",
        )
        await settle_session(runtime, self.SESSION_ID)

        restored = ToolSurface(providers=(FakeEchoProvider(),))
        restored_runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(()),
            load_toolset_binding(restored),
            SequentialIds(),
        )
        restored.attach(restored_runtime)
        activations = await restored.rehydrate(
            self.SESSION_ID,
            await runtime.snapshot(self.SESSION_ID),
        )

        self.assertEqual(activations, ())
        self.assertEqual(
            _schema_names(restored.schemas(self.SESSION_ID)),
            {LOAD_TOOLSET},
        )

    async def test_rehydrate_rejects_silent_toolset_revision_upgrade(self):
        events = await self._committed_load_events()
        surface = ToolSurface(providers=(FakeEchoProvider(revision=2),))
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(()),
            load_toolset_binding(surface),
            SequentialIds(),
        )
        surface.attach(runtime)

        with self.assertRaises(ToolsetLoadError) as raised:
            await surface.rehydrate(self.SESSION_ID, events)

        self.assertEqual(raised.exception.code, "TOOLSET_REVISION_UNAVAILABLE")
        self.assertEqual(
            _schema_names(surface.schemas(self.SESSION_ID)),
            {LOAD_TOOLSET},
        )

    async def test_dynamic_tool_freezes_host_authorization_requirement(self):
        surface = ToolSurface(
            providers=(
                FakeEchoProvider(
                    requires_authorization=True,
                ),
            )
        )
        model = ScriptedDecisionMaker(
            (
                lambda _frame: ModelDecision(
                    content="ping",
                    command_requests=(InvokeTool("demo_ping"),),
                ),
            )
        )
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            load_toolset_binding(surface),
            SequentialIds(),
        )
        surface.attach(runtime)
        loaded = await surface.load(self.SESSION_ID, "demo")
        self.assertTrue(loaded["ok"])
        await runtime.receive_user_message(
            self.SESSION_ID,
            "ping",
            delivery_id="auth-1",
        )

        step = (await runtime.advance(self.SESSION_ID)).step
        state = await runtime.state(self.SESSION_ID)

        self.assertTrue(step.commands[0].requires_authorization)
        self.assertEqual(
            state.waiting_for,
            (f"authorization:{step.commands[0].command_id}",),
        )
