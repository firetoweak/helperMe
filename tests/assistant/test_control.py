from __future__ import annotations

import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict

from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.assembly import build_assistant_assembly
from helperme.assistant.decision import JournalBackedLlmDecisionMaker
from helperme.assistant.delivery import deliver_binding
from helperme.assistant.toolsets import ToolSurface
from tests.session_scheduler import settle_session
from helperme.llm.types import (
    LLMCallResult,
    LLMResponse,
    LLMUsage,
    ToolCall,
)
from helperme.config import AssistantConfig
from helperme.paths import HelperMeHome
from helperme.runtime import AgentRuntime, MemoryJournal, StepCommitted
from helperme.tools.control import (
    ControlApprovalExecution,
    ControlApprovalRequest,
)
from helperme.tools.spec import PydanticParameters, ToolSpec


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class ControlLlm:
    async def chat(self, _messages, _model, *, tools=None):
        names = {tool["function"]["name"] for tool in tools}
        if "propose_test_control" not in names:
            raise AssertionError("control schema was not offered")
        return LLMCallResult(
            LLMResponse(
                content="我已整理方案。",
                calls=(
                    ToolCall(
                        "control-1",
                        "propose_test_control",
                        '{"value":"frozen"}',
                    ),
                ),
            ),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class ApprovalHandler:
    action = "test.install"

    def __init__(self) -> None:
        self.payloads: list[Mapping[str, object]] = []

    async def execute(self, payload):
        self.payloads.append(payload)
        return ControlApprovalExecution(True, "安装完成")


class FailingApprovalHandler(ApprovalHandler):
    async def execute(self, payload):
        self.payloads.append(payload)
        raise RuntimeError("approval execution failed")


class EmptySkillTools:
    def schemas(self):
        return []


class OpenControlManagement:
    def schemas(self, _session_id, _state):
        return []

    def control_names(self, _session_id, _state):
        return frozenset({"propose_test_control"})

    def catalog_instruction(self, _session_id, _state):
        return "test management"


def _decision_maker(journal, llm, control):
    return JournalBackedLlmDecisionMaker(
        journal,
        llm,
        "test-model",
        surface=ToolSurface(),
        skill_tools=EmptySkillTools(),
        control=control,
        management=OpenControlManagement(),
    )


class ConversationalControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_assembly_offers_mcp_and_skill_controls_outside_runtime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            home = HelperMeHome(root / ".helperme")
            config = AssistantConfig(
                model_name="test-model",
                workspace_root=workspace,
                full_access=False,
                model_context_limit=1000,
                input_budget_ratio=0.8,
                llm=ControlLlm(),
            )
            with patch(
                "helperme.assistant.assembly.HelperMeHome.default",
                return_value=home,
            ):
                assembly = await build_assistant_assembly(
                    config,
                    lambda _text: None,
                    MemoryJournal(),
                )

        names = assembly.control.names()
        self.assertEqual(
            names,
            {
                "propose_mcp_install",
                "propose_mcp_recovery",
                "propose_mcp_update",
                "propose_skill_install",
                "propose_skill_enable",
                "propose_skill_update",
                "propose_skill_repair",
            },
        )
        self.assertTrue(names.isdisjoint(assembly.bindings))
        self.assertTrue(
            {
                "list_mcp_servers",
                "test_mcp_server",
                "list_installed_skills",
                "inspect_installed_skill",
                "test_installed_skill",
            }.issubset(assembly.bindings)
        )
        await assembly.scheduler.close()

    async def test_proposal_runs_only_after_step_commit_then_waits_for_yes(self):
        journal = MemoryJournal()
        proposal_saw_committed_step: list[bool] = []

        async def propose(input_data: ProposalInput):
            events = await journal.snapshot("control-session")
            proposal_saw_committed_step.append(
                any(isinstance(event.payload, StepCommitted) for event in events)
            )
            return ControlApprovalRequest(
                id="approval-1",
                action="test.install",
                payload={"value": input_data.value},
                summary="安装 frozen",
                risk="测试风险",
            )

        spec = ToolSpec(
            "propose_test_control",
            "提交测试控制方案。",
            PydanticParameters(ProposalInput),
            propose,
            control_boundary=True,
            exclusive_batch=True,
        )
        handler = ApprovalHandler()
        control = AssistantControlPlane((spec,), (handler,))
        delivered: list[str] = []
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, ControlLlm(), control),
            deliver_binding(delivered.append),
        )
        await runtime.receive_user_message(
            "control-session",
            "安装它",
            delivery_id="user-1",
        )

        result = await settle_session(
            runtime,
            "control-session",
            control=control,
        )

        self.assertEqual(proposal_saw_committed_step, [True])
        self.assertEqual(delivered, ["我已整理方案。"])
        self.assertIn("输入 yes 确认", result.control_message)
        self.assertEqual(
            control.pending_view("control-session").request_id,
            "approval-1",
        )
        steps = [
            event.payload.step
            for event in await journal.snapshot("control-session")
            if isinstance(event.payload, StepCommitted)
        ]
        self.assertEqual(
            [command.effect.name for command in steps[0].commands],
            ["deliver"],
        )

        message = await control.resolve("control-session", approved=True)

        self.assertEqual(message, "安装完成")
        self.assertEqual(dict(handler.payloads[0]), {"value": "frozen"})
        self.assertIsNone(control.pending_view("control-session"))

    async def test_approval_is_consumed_before_execution_failure(self):
        journal = MemoryJournal()

        async def propose(input_data: ProposalInput):
            return ControlApprovalRequest(
                id="approval-1",
                action="test.install",
                payload={"value": input_data.value},
                summary="安装 frozen",
                risk="测试风险",
            )

        spec = ToolSpec(
            "propose_test_control",
            "提交测试控制方案。",
            PydanticParameters(ProposalInput),
            propose,
            control_boundary=True,
            exclusive_batch=True,
        )
        handler = FailingApprovalHandler()
        control = AssistantControlPlane((spec,), (handler,))
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, ControlLlm(), control),
            deliver_binding(lambda _text: None),
        )
        await runtime.receive_user_message(
            "control-session",
            "安装它",
            delivery_id="user-1",
        )
        await settle_session(
            runtime,
            "control-session",
            control=control,
        )

        with self.assertRaisesRegex(RuntimeError, "approval execution failed"):
            await control.resolve("control-session", approved=True)

        self.assertIsNone(control.pending_view("control-session"))


if __name__ == "__main__":
    unittest.main()
