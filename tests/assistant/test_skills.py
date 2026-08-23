from __future__ import annotations

import tempfile
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path

from helperme.assistant.artifacts import MemoryArtifactGateway
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.context.projection import ModelContextSettings
from helperme.assistant.runner import drive_until_idle
from helperme.assistant.skills import SkillToolAdapter
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
from helperme.paths import HelperMeHome
from helperme.skills.application import SkillApplicationService
from helperme.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE
from tests.skills.test_package import write_skill


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


def _schema_names(schemas: list[dict[str, object]]) -> set[str]:
    names: set[str] = set()
    for schema in schemas:
        function = schema.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


def _load_skill_description(schemas: list[dict[str, object]]) -> str:
    for schema in schemas:
        function = schema.get("function")
        if isinstance(function, dict) and function.get("name") == LOAD_SKILL:
            return str(function.get("description") or "")
    return ""


class SkillToolAdapterTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "skill-stream"

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = HelperMeHome(root / ".helperme")
        self.workspace.initialize()
        source = root / "source"
        write_skill(
            source,
            name="demo",
            description="Demo workflow",
            body="\nFollow the demo workflow.\n",
        )
        reference = source / "references" / "guide.md"
        reference.parent.mkdir()
        reference.write_text("abcdefghij", encoding="utf-8")
        self.service = SkillApplicationService(self.workspace)
        await self.service.install_local(source)
        await self.service.set_enabled("demo", True)
        self.adapter = SkillToolAdapter(
            type(
                "Skills",
                (),
                {"tool_catalog": self.service.tool_catalog},
            )(),
            MemoryArtifactGateway(),
            ModelContextSettings(),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_enabled_catalog_is_in_load_skill_description(self):
        schemas = self.adapter.schemas()
        self.assertEqual(
            _schema_names(schemas),
            {LOAD_SKILL, READ_SKILL_RESOURCE},
        )
        self.assertIn("demo: Demo workflow", _load_skill_description(schemas))

    async def test_disable_removes_skill_tools_from_the_next_decision(self):
        await self.service.set_enabled("demo", False)
        self.assertEqual(self.adapter.schemas(), [])

    async def test_load_skill_returns_main_instructions_as_a_tool_result(self):
        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="loading skill",
                    command_requests=(
                        InvokeTool(LOAD_SKILL, (("skill_id", "demo"),)),
                    ),
                ),
                lambda _frame: ModelDecision(
                    content="loaded",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "loaded"),)),
                    ),
                ),
            )),
            {
                **self.adapter.bindings(),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "use demo skill",
            delivery_id="ask-1",
        )
        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
        )
        events = await runtime.snapshot(self.STREAM_ID)
        loaded = _outcomes_named(events, LOAD_SKILL)
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(delivered, ["loaded"])
        self.assertEqual(loaded[0]["code"], "SKILL_LOADED")
        self.assertEqual(
            loaded[0]["data"]["content"],
            "\nFollow the demo workflow.\n",
        )

    async def test_read_skill_resource_does_not_require_a_prior_load(self):
        delivered: list[str] = []
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="reading",
                    command_requests=(
                        InvokeTool(
                            READ_SKILL_RESOURCE,
                            (
                                ("skill_id", "demo"),
                                ("relative_path", "references/guide.md"),
                                ("offset", 2),
                                ("limit", 4),
                            ),
                        ),
                    ),
                ),
                lambda _frame: ModelDecision(
                    content="ok",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "ok"),)),
                    ),
                ),
            )),
            {
                **self.adapter.bindings(),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "read guide",
            delivery_id="ask-1",
        )
        await drive_until_idle(runtime, self.STREAM_ID)
        events = await runtime.snapshot(self.STREAM_ID)
        reads = _outcomes_named(events, READ_SKILL_RESOURCE)
        self.assertEqual(reads[0]["data"]["content"], "cdef")


def _outcomes_named(events, name: str) -> list[object]:
    names: dict[str, str] = {}
    for event in events:
        payload = event.payload
        if isinstance(payload, StepCommitted):
            for command in payload.step.commands:
                effect = command.effect
                if isinstance(effect, InvokeTool):
                    names[command.command_id] = effect.name
    return [
        event.payload.outcome.value
        for event in events
        if isinstance(event.payload, CommandOutcomeReceived)
        and names.get(event.payload.command_id) == name
    ]
