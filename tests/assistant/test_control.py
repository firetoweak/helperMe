from __future__ import annotations

import unittest
from collections.abc import Mapping
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from helperme.assistant.artifacts import MemoryArtifactStore
from helperme.assistant.context.projection import ModelContextProjector
from helperme.assistant.control import AssistantControlPlane
from helperme.assistant.decision import JournalBackedLlmDecisionMaker
from helperme.assistant.delivery import deliver_binding
from helperme.assistant.toolsets import ToolSurface
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall
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
from tests.session_scheduler import settle_session


SESSION_ID = "control-session"
PROPOSAL_NAME = "propose_test_control"


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class ApprovalHandler:
    action = "test.install"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.payloads: list[Mapping[str, object]] = []

    async def execute(self, payload):
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("approval execution failed")
        return ControlApprovalExecution(True, "安装完成")


class ControlLlm:
    def __init__(self, *, control_calls: int = 1) -> None:
        self.control_calls = control_calls
        self.calls = 0

    async def chat(self, _messages, _model, *, tools=None):
        self.calls += 1
        names = {tool["function"]["name"] for tool in tools}
        if PROPOSAL_NAME not in names:
            raise AssertionError("stale stage hid control schema")
        calls = ()
        if self.calls <= self.control_calls:
            calls = (
                ToolCall("control-1", PROPOSAL_NAME, '{"value":"frozen"}'),
            )
        return LLMCallResult(
            LLMResponse(content="done", calls=calls),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class EmptySkillTools:
    def schemas(self):
        return []


class OpenControlManagement:
    def schemas(self, _session_id, _state):
        return []

    def control_names(self, _session_id, _state):
        return frozenset({PROPOSAL_NAME})

    def catalog_instruction(self, _session_id, _state):
        return "test management"


class FailOnceJournal(MemoryJournal):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    async def commit_step(self, lease, draft):
        if self.mode:
            mode, self.mode = self.mode, ""
            if mode == "lease":
                raise LeaseLostError(lease.token)
            raise RuntimeError("commit failed")
        return await super().commit_step(lease, draft)


class SaveFailingStore(MemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def save(self, content):
        if self.fail:
            self.fail = False
            raise RuntimeError("decision evidence save failed")
        return super().save(content)


class SaveFailingGateway:
    def __init__(self) -> None:
        self.store = SaveFailingStore()

    def for_session(self, _session_id):
        return self.store


def _frame(*, trigger: str = "trigger-1", cursor: int = 1, basis: str = "basis-1"):
    return SimpleNamespace(
        state=SimpleNamespace(session_id=SESSION_ID),
        trigger_event=SimpleNamespace(event_id=trigger),
        decision_cursor=cursor,
        basis_state_version=basis,
    )


def _step(*, trigger: str = "trigger-1", cursor: int = 1, basis: str = "basis-1"):
    return SimpleNamespace(
        trigger_event_id=trigger,
        decision_cursor=cursor,
        basis_state_version=basis,
    )


def _operation(propose, handler: ApprovalHandler | None = None) -> ControlOperation:
    return ControlOperation(
        "test",
        ToolSpec(
            PROPOSAL_NAME,
            "提交测试控制方案。",
            PydanticParameters(ProposalInput),
            propose,
            control_boundary=True,
            exclusive_batch=True,
        ),
        ApprovalHandler() if handler is None else handler,
    )


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


class ConversationalControlTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _control_that_must_not_run() -> AssistantControlPlane:
        async def stale(_input: ProposalInput):
            raise AssertionError("stale proposal must not execute")

        return AssistantControlPlane((_operation(stale),))

    async def _assert_retry_clears_stage(
        self,
        journal: MemoryJournal,
        *,
        projector=None,
        expected_error: str | None = None,
    ) -> None:
        control = self._control_that_must_not_run()
        llm = ControlLlm()
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, llm, control, projector=projector),
            deliver_binding(lambda _text: None),
        )
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")

        if expected_error is None:
            first = await runtime.advance(SESSION_ID)
            self.assertIsNone(first.step)
            self.assertIs(first.status, RuntimeStatus.RUNNABLE)
        else:
            with self.assertRaisesRegex(RuntimeError, expected_error):
                await runtime.advance(SESSION_ID)
        await settle_session(runtime, SESSION_ID, control=control)

        self.assertGreaterEqual(llm.calls, 2)
        self.assertIsNone(control.pending_view(SESSION_ID))
        self.assertEqual(len(control.schemas(SESSION_ID)), 1)

    async def test_decision_failure_stage_is_cleared_before_retry(self):
        await self._assert_retry_clears_stage(
            MemoryJournal(),
            projector=ModelContextProjector(gateway=SaveFailingGateway()),
            expected_error="evidence save failed",
        )

    async def test_step_commit_failure_stage_is_cleared_before_retry(self):
        await self._assert_retry_clears_stage(
            FailOnceJournal("commit"),
            expected_error="commit failed",
        )

    async def test_lease_lost_stage_is_cleared_before_retry(self):
        await self._assert_retry_clears_stage(FailOnceJournal("lease"))

    async def test_proposal_commits_before_approval_and_consumes_once(self):
        journal = MemoryJournal()
        committed: list[bool] = []

        async def propose(input_data: ProposalInput):
            events = await journal.snapshot(SESSION_ID)
            committed.append(any(isinstance(e.payload, StepCommitted) for e in events))
            return ControlApprovalRequest(
                "approval-1",
                "test.install",
                {"value": input_data.value},
                "安装 frozen",
                "测试风险",
            )

        handler = ApprovalHandler()
        control = AssistantControlPlane((_operation(propose, handler),))
        delivered: list[str] = []
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, ControlLlm(), control),
            deliver_binding(delivered.append),
        )
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")

        result = await settle_session(runtime, SESSION_ID, control=control)

        self.assertEqual(committed, [True])
        self.assertEqual(delivered, ["done"])
        self.assertIn("输入 yes 确认", result.control_message)
        self.assertEqual(control.pending_view(SESSION_ID).request_id, "approval-1")
        steps = [
            event.payload.step
            for event in await journal.snapshot(SESSION_ID)
            if isinstance(event.payload, StepCommitted)
        ]
        self.assertEqual([c.effect.name for c in steps[0].commands], ["deliver"])

        self.assertEqual(await control.resolve(SESSION_ID, approved=True), "安装完成")
        self.assertEqual(dict(handler.payloads[0]), {"value": "frozen"})
        self.assertIsNone(control.pending_view(SESSION_ID))

    async def test_approval_failure_does_not_restore_consumed_request(self):
        async def propose(input_data: ProposalInput):
            return ControlApprovalRequest(
                "approval-1",
                "test.install",
                {"value": input_data.value},
                "安装 frozen",
                "测试风险",
            )

        handler = ApprovalHandler(fail=True)
        control = AssistantControlPlane((_operation(propose, handler),))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, ControlLlm(), control),
            deliver_binding(lambda _text: None),
        )
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await settle_session(runtime, SESSION_ID, control=control)

        with self.assertRaisesRegex(RuntimeError, "approval execution failed"):
            await control.resolve(SESSION_ID, approved=True)
        self.assertIsNone(control.pending_view(SESSION_ID))

    async def test_restart_discards_unconfirmed_approval(self):
        handler = ApprovalHandler()

        async def propose(_input: ProposalInput):
            return ControlApprovalRequest(
                "approval-1", "test.install", {}, "安装 frozen", "测试风险"
            )

        operation = _operation(propose, handler)
        original = AssistantControlPlane((operation,))
        original.stage(_frame(), PROPOSAL_NAME, {"value": "frozen"})
        await original.after_committed_step(SESSION_ID, _step())

        restarted = AssistantControlPlane((operation,))

        self.assertIsNotNone(original.pending_view(SESSION_ID))
        self.assertIsNone(restarted.pending_view(SESSION_ID))
        self.assertEqual(handler.payloads, [])

    async def test_unmatched_step_discards_staged_call(self):
        async def propose(_input: ProposalInput):
            return {"ok": True}

        control = AssistantControlPlane((_operation(propose),))
        control.stage(_frame(), PROPOSAL_NAME, {"value": "frozen"})

        self.assertIsNone(
            await control.after_committed_step(
                SESSION_ID,
                _step(trigger="trigger-2", cursor=2, basis="basis-2"),
            )
        )
        self.assertIsNone(await control.after_committed_step(SESSION_ID, _step()))
        self.assertEqual(len(control.schemas(SESSION_ID)), 1)

    async def test_proposal_action_must_match_operation(self):
        async def propose(_input: ProposalInput):
            return ControlApprovalRequest(
                "approval-1", "test.wrong", {}, "bad", "bad"
            )

        control = AssistantControlPlane((_operation(propose),))
        control.stage(_frame(), PROPOSAL_NAME, {"value": "frozen"})

        with self.assertRaisesRegex(ValueError, "proposal action 不匹配"):
            await control.after_committed_step(SESSION_ID, _step())
        self.assertEqual(len(control.schemas(SESSION_ID)), 1)
