from __future__ import annotations

import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict

from helperme.assistant.artifacts import MemoryArtifactStore
from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.context.projection import ModelContextProjector
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
from helperme.runtime import (
    AgentRuntime,
    LeaseLostError,
    MemoryJournal,
    RuntimeStatus,
    StepCommitted,
)
from helperme.tools.control import (
    ControlApprovalExecution,
    ControlApprovalRequest,
    ControlOperation,
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


def _decision_maker(journal, llm, control, *, projector=None):
    return JournalBackedLlmDecisionMaker(
        journal,
        llm,
        "test-model",
        surface=ToolSurface(),
        skill_tools=EmptySkillTools(),
        control=control,
        management=OpenControlManagement(),
        projector=projector,
    )


class RetryAfterLeaseLossLlm:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, _messages, _model, *, tools=None):
        self.calls += 1
        names = {tool["function"]["name"] for tool in tools}
        if "propose_test_control" not in names:
            raise AssertionError("stale stage hid control schema on retry")
        calls = ()
        if self.calls == 1:
            calls = (
                ToolCall(
                    "control-1",
                    "propose_test_control",
                    '{"value":"frozen"}',
                ),
            )
        return LLMCallResult(
            LLMResponse(content="done", calls=calls),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class LeaseLosingJournal(MemoryJournal):
    def __init__(self) -> None:
        super().__init__()
        self._lose_once = True

    async def commit_step(self, lease, draft):
        if self._lose_once:
            self._lose_once = False
            raise LeaseLostError(lease.token)
        return await super().commit_step(lease, draft)


class CommitFailingJournal(MemoryJournal):
    def __init__(self) -> None:
        super().__init__()
        self._fail_once = True

    async def commit_step(self, lease, draft):
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("commit failed")
        return await super().commit_step(lease, draft)


class SaveFailingStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self._fail_once = True

    def save(self, content):
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("decision evidence save failed")
        return super().save(content)


class SaveFailingGateway:
    def __init__(self) -> None:
        self.store = SaveFailingStore()

    def for_session(self, _session_id):
        return self.store


class ConversationalControlTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _control_that_must_not_run() -> AssistantControlPlane:
        async def propose(_input: ProposalInput):
            raise AssertionError("stale proposal must not execute")

        spec = ToolSpec(
            "propose_test_control",
            "提交测试控制方案。",
            PydanticParameters(ProposalInput),
            propose,
            control_boundary=True,
            exclusive_batch=True,
        )
        return AssistantControlPlane(
            (ControlOperation("test", spec, ApprovalHandler()),)
        )

    async def test_decision_failure_stage_is_cleared_before_retry(self):
        control = self._control_that_must_not_run()
        journal = MemoryJournal()
        llm = RetryAfterLeaseLossLlm()
        projector = ModelContextProjector(gateway=SaveFailingGateway())
        runtime = AgentRuntime(
            journal,
            _decision_maker(
                journal,
                llm,
                control,
                projector=projector,
            ),
            deliver_binding(lambda _text: None),
        )
        await runtime.receive_user_message(
            "control-session",
            "安装它",
            delivery_id="user-1",
        )

        with self.assertRaisesRegex(RuntimeError, "evidence save failed"):
            await runtime.advance("control-session")
        await settle_session(runtime, "control-session", control=control)

        self.assertIsNone(control.pending_view("control-session"))
        self.assertEqual(len(control.schemas("control-session")), 1)

    async def test_step_commit_failure_stage_is_cleared_before_retry(self):
        control = self._control_that_must_not_run()
        journal = CommitFailingJournal()
        llm = RetryAfterLeaseLossLlm()
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, llm, control),
            deliver_binding(lambda _text: None),
        )
        await runtime.receive_user_message(
            "control-session",
            "安装它",
            delivery_id="user-1",
        )

        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            await runtime.advance("control-session")
        await settle_session(runtime, "control-session", control=control)

        self.assertIsNone(control.pending_view("control-session"))
        self.assertEqual(len(control.schemas("control-session")), 1)

    async def test_lease_lost_stage_is_cleared_before_retry(self):
        control = self._control_that_must_not_run()
        journal = LeaseLosingJournal()
        llm = RetryAfterLeaseLossLlm()
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, llm, control),
            deliver_binding(lambda _text: None),
        )
        await runtime.receive_user_message(
            "control-session",
            "安装它",
            delivery_id="user-1",
        )

        first = await runtime.advance("control-session")
        self.assertIsNone(first.step)
        self.assertIs(first.status, RuntimeStatus.RUNNABLE)

        await settle_session(runtime, "control-session", control=control)

        self.assertGreaterEqual(llm.calls, 2)
        self.assertIsNone(control.pending_view("control-session"))
        self.assertEqual(len(control.schemas("control-session")), 1)

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
        self.assertTrue(names.issubset(assembly.surface._reserved))
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
        control = AssistantControlPlane((ControlOperation("test", spec, handler),))
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
        control = AssistantControlPlane((ControlOperation("test", spec, handler),))
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

    async def test_unmatched_committed_step_clears_staged_call(self):
        async def propose(_input: ProposalInput):
            return {"ok": True}

        spec = ToolSpec(
            "propose_test_control",
            "提交测试控制方案。",
            PydanticParameters(ProposalInput),
            propose,
            control_boundary=True,
            exclusive_batch=True,
        )
        control = AssistantControlPlane(
            (ControlOperation("test", spec, ApprovalHandler()),)
        )
        frame = SimpleNamespace(
            state=SimpleNamespace(session_id="control-session"),
            trigger_event=SimpleNamespace(event_id="trigger-1"),
            decision_cursor=1,
            basis_state_version="basis-1",
        )
        control.stage(frame, spec.name, {"value": "frozen"})

        notice = await control.after_committed_step(
            "control-session",
            SimpleNamespace(
                trigger_event_id="trigger-2",
                decision_cursor=2,
                basis_state_version="basis-2",
            ),
        )

        self.assertIsNone(notice)
        self.assertEqual(len(control.schemas("control-session")), 1)

    async def test_proposal_action_must_match_its_operation(self):
        async def propose(_input: ProposalInput):
            return ControlApprovalRequest(
                id="approval-1",
                action="test.wrong",
                payload={},
                summary="bad",
                risk="bad",
            )

        spec = ToolSpec(
            "propose_test_control",
            "提交测试控制方案。",
            PydanticParameters(ProposalInput),
            propose,
            control_boundary=True,
            exclusive_batch=True,
        )
        control = AssistantControlPlane(
            (ControlOperation("test", spec, ApprovalHandler()),)
        )
        frame = SimpleNamespace(
            state=SimpleNamespace(session_id="control-session"),
            trigger_event=SimpleNamespace(event_id="trigger-1"),
            decision_cursor=1,
            basis_state_version="basis-1",
        )
        control.stage(frame, spec.name, {"value": "frozen"})

        with self.assertRaisesRegex(ValueError, "proposal action 不匹配"):
            await control.after_committed_step(
                "control-session",
                SimpleNamespace(
                    trigger_event_id="trigger-1",
                    decision_cursor=1,
                    basis_state_version="basis-1",
                ),
            )

        self.assertEqual(len(control.schemas("control-session")), 1)


if __name__ == "__main__":
    unittest.main()
