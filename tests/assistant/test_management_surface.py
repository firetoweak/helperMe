from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict

from helperme.assistant.artifacts import FileArtifactGateway
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.management import (
    LOAD_MANAGEMENT_TOOLS,
    ManagementDomain,
    ManagementSurface,
)
from helperme.assistant.context.projection import ModelContextSettings
from tests.session_scheduler import settle_session
from helperme.runtime import AgentRuntime, InvokeTool, MemoryJournal, ModelDecision
from helperme.runtime.state import DecisionFrame
from helperme.tools.spec import PydanticParameters, ToolSpec


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _diagnose(_input: EmptyInput) -> dict[str, object]:
    return {"ok": True, "code": "HEALTHY", "data": {}}


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name,
        f"{name} description",
        PydanticParameters(EmptyInput),
        _diagnose,
    )


def _names(schemas: list[dict[str, object]]) -> set[str]:
    return {schema["function"]["name"] for schema in schemas}


class ScriptedDecisionMaker:
    def __init__(self, management: ManagementSurface) -> None:
        self.management = management
        self.seen: list[set[str]] = []
        self.control_seen: list[frozenset[str]] = []

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        session_id = frame.state.session_id
        self.seen.append(_names(self.management.schemas(session_id, frame.state)))
        self.control_seen.append(self.management.control_names(session_id, frame.state))
        if len(self.seen) == 1:
            return ModelDecision(
                content="load mcp twice",
                command_requests=(
                    InvokeTool(
                        LOAD_MANAGEMENT_TOOLS,
                        (("domain", "mcp"),),
                    ),
                    InvokeTool(
                        LOAD_MANAGEMENT_TOOLS,
                        (("domain", "mcp"),),
                    ),
                ),
            )
        return ModelDecision(
            content="done",
            command_requests=(InvokeTool(DELIVER_TOOL_NAME, (("text", "done"),)),),
        )


class ManagementProgressiveLoadTest(unittest.IsolatedAsyncioTestCase):
    async def test_domain_tools_appear_next_step_and_rehydrate(self):
        with TemporaryDirectory() as directory:
            gateway = FileArtifactGateway(Path(directory))
            domains = (
                ManagementDomain(
                    "mcp",
                    "mcp management",
                    (_spec("diagnose_mcp"),),
                    ("propose_mcp_repair",),
                ),
                ManagementDomain(
                    "skill",
                    "skill management",
                    (_spec("diagnose_skill"),),
                    ("propose_skill_repair",),
                ),
            )
            management = ManagementSurface(
                domains,
                gateway,
                ModelContextSettings(),
            )
            decisions = ScriptedDecisionMaker(management)
            delivered: list[str] = []
            runtime = AgentRuntime(
                MemoryJournal(),
                decisions,
                {
                    **management.bindings(),
                    **deliver_binding(delivered.append),
                },
            )
            await runtime.receive_user_message(
                "management-session",
                "repair mcp",
                delivery_id="user-1",
            )

            await settle_session(runtime, "management-session")
            events = await runtime.snapshot("management-session")

            self.assertEqual(decisions.seen[0], {LOAD_MANAGEMENT_TOOLS})
            self.assertEqual(
                decisions.seen[1],
                {LOAD_MANAGEMENT_TOOLS, "diagnose_mcp"},
            )
            self.assertEqual(
                decisions.control_seen[1],
                frozenset({"propose_mcp_repair"}),
            )
            self.assertNotIn("diagnose_skill", decisions.seen[1])
            self.assertEqual(delivered, ["done"])

            restored = ManagementSurface(
                domains,
                gateway,
                ModelContextSettings(),
            )
            activations = await restored.rehydrate(
                "management-session",
                events,
            )

            self.assertEqual(len(activations), 2)
            self.assertEqual(
                _names(restored.schemas("management-session")),
                {LOAD_MANAGEMENT_TOOLS, "diagnose_mcp"},
            )
            self.assertEqual(
                restored.control_names("management-session"),
                frozenset({"propose_mcp_repair"}),
            )

    async def test_failed_load_outcome_does_not_create_activation(self):
        with TemporaryDirectory() as directory:
            gateway = FileArtifactGateway(Path(directory))
            domains = (
                ManagementDomain(
                    "mcp",
                    "mcp management",
                    (_spec("diagnose_mcp"),),
                    ("propose_mcp_repair",),
                ),
            )
            management = ManagementSurface(
                domains,
                gateway,
                ModelContextSettings(),
            )

            class MissingDomainDecisionMaker:
                def __init__(self) -> None:
                    self.calls = 0

                async def decide(self, _frame: DecisionFrame) -> ModelDecision:
                    self.calls += 1
                    if self.calls == 1:
                        return ModelDecision(
                            command_requests=(
                                InvokeTool(
                                    LOAD_MANAGEMENT_TOOLS,
                                    (("domain", "missing"),),
                                ),
                            ),
                        )
                    return ModelDecision(
                        command_requests=(
                            InvokeTool(
                                DELIVER_TOOL_NAME,
                                (("text", "done"),),
                            ),
                        ),
                    )

            runtime = AgentRuntime(
                MemoryJournal(),
                MissingDomainDecisionMaker(),
                {
                    **management.bindings(),
                    **deliver_binding(lambda _text: None),
                },
            )
            await runtime.receive_user_message(
                "management-session",
                "load missing",
                delivery_id="missing-1",
            )
            await settle_session(runtime, "management-session")

            restored = ManagementSurface(
                domains,
                gateway,
                ModelContextSettings(),
            )
            self.assertEqual(
                await restored.rehydrate(
                    "management-session",
                    await runtime.snapshot("management-session"),
                ),
                (),
            )
            self.assertEqual(
                _names(restored.schemas("management-session")),
                {LOAD_MANAGEMENT_TOOLS},
            )


if __name__ == "__main__":
    unittest.main()
