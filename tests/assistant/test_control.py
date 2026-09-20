from __future__ import annotations

import unittest
from collections.abc import Mapping
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict

from helperme.assistant.artifacts import MemoryArtifactStore
from helperme.assistant.context.projection import ModelContextProjector
from helperme.assistant.control import (
    CONTROL_CONCLUDED,
    CONTROL_FAILED,
    CONTROL_PROPOSED,
    CONTROL_RESOLVED,
    AssistantControlPlane,
    project_pending_approval,
)
from helperme.assistant.decision import JournalBackedLlmDecisionMaker
from helperme.assistant.sessions import AssistantSessions
from helperme.assistant.delivery import deliver_binding
from helperme.assistant.toolsets import ToolSurface
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from helperme.runtime import (
    AgentRuntime,
    DomainFactCommitted,
    Event,
    LeaseLostError,
    MemoryJournal,
    RuntimeStatus,
    StepCommitted,
)
from helperme.runtime.json_values import thaw_value
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


class EmptyCliTools:
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


def _facts(events, fact_type: str) -> list[DomainFactCommitted]:
    return [
        event.payload
        for event in events
        if isinstance(event.payload, DomainFactCommitted)
        and event.payload.fact_type == fact_type
    ]


async def _ignore_wake(_session_id: str) -> None:
    return None


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
        cli_tools=EmptyCliTools(),
        control=control,
        management=OpenControlManagement(),
        projector=projector,
    )


def _event(sequence: int, payload) -> Event:
    from datetime import datetime, timezone

    return Event(
        event_id=f"event-{sequence}",
        session_id=SESSION_ID,
        sequence=sequence,
        payload=payload,
        occurred_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        causation_id=None,
        correlation_id=None,
        schema_version=5,
        artifact_refs=(),
    )


def _proposed(request_id: str = "approval-1") -> DomainFactCommitted:
    return DomainFactCommitted(
        CONTROL_PROPOSED,
        {
            "request_id": request_id,
            "action": "test.install",
            "payload": {"value": "frozen"},
            "summary": "安装 frozen",
            "risk": "测试风险",
        },
    )


class ProjectPendingApprovalTest(unittest.TestCase):
    def test_proposed_is_pending_until_resolved(self):
        proposed = _event(1, _proposed())
        self.assertEqual(project_pending_approval((proposed,)).id, "approval-1")
        resolved = _event(
            2,
            DomainFactCommitted(
                CONTROL_RESOLVED,
                {
                    "request_id": "approval-1",
                    "approved": True,
                    "action": "test.install",
                    "succeeded": True,
                    "message": "安装完成",
                    "data": {},
                },
            ),
        )
        self.assertIsNone(project_pending_approval((proposed, resolved)))

    def test_failed_and_concluded_are_not_pending(self):
        self.assertIsNone(
            project_pending_approval(
                (
                    _event(
                        1,
                        DomainFactCommitted(
                            CONTROL_FAILED,
                            {"tool": PROPOSAL_NAME, "error": "probe unreachable"},
                        ),
                    ),
                )
            )
        )
        self.assertIsNone(
            project_pending_approval(
                (
                    _event(
                        1,
                        DomainFactCommitted(
                            CONTROL_CONCLUDED,
                            {"tool": PROPOSAL_NAME, "result": {"ok": False}},
                        ),
                    ),
                )
            )
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
            deliver_binding(lambda _session_id, _output_id, _text: None),
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

        events = await journal.snapshot(SESSION_ID)
        self.assertGreaterEqual(llm.calls, 2)
        self.assertIsNone(project_pending_approval(events))
        self.assertEqual(len(control.schemas(SESSION_ID, events)), 1)

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
            deliver_binding(lambda _session_id, _output_id, text: delivered.append(text)),
        )
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")

        result = await settle_session(runtime, SESSION_ID, control=control)

        self.assertEqual(committed, [True])
        self.assertEqual(delivered, ["done"])
        self.assertIsNone(result.control_message)
        events = await journal.snapshot(SESSION_ID)
        request = project_pending_approval(events)
        self.assertEqual(request.id, "approval-1")
        self.assertEqual(dict(request.payload), {"value": "frozen"})
        steps = [
            event.payload.step
            for event in events
            if isinstance(event.payload, StepCommitted)
        ]
        self.assertEqual([c.effect.name for c in steps[0].commands], ["deliver"])

        resolved = await control.resolve(request, approved=True)
        self.assertEqual(resolved.message, "安装完成")
        self.assertEqual(resolved.action, "test.install")
        self.assertTrue(resolved.approved)
        self.assertTrue(resolved.succeeded)
        self.assertEqual(dict(handler.payloads[0]), {"value": "frozen"})

    async def test_approval_writes_control_fact_and_requests_decision(self):
        async def propose(input_data: ProposalInput):
            return ControlApprovalRequest(
                "approval-1",
                "test.install",
                {"value": input_data.value},
                "安装 frozen",
                "测试风险",
            )

        handler = ApprovalHandler()
        control = AssistantControlPlane((_operation(propose, handler),))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, ControlLlm(), control),
            deliver_binding(lambda _session_id, _output_id, _text: None),
        )
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await settle_session(runtime, SESSION_ID, control=control)

        woken: list[str] = []

        async def wake(session_id: str) -> None:
            woken.append(session_id)

        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            SimpleNamespace(wake=wake),
            control=control,
            management=SimpleNamespace(),
        )
        message = await sessions.resolve_control(SESSION_ID, approved=True)

        self.assertEqual(message, "安装完成")
        self.assertEqual(woken, [SESSION_ID])
        facts = _facts(await journal.snapshot(SESSION_ID), CONTROL_RESOLVED)
        self.assertEqual(len(facts), 1)
        self.assertTrue(facts[0].requests_decision)
        self.assertEqual(
            thaw_value(facts[0].data),
            {
                "request_id": "approval-1",
                "approved": True,
                "action": "test.install",
                "succeeded": True,
                "message": "安装完成",
                "data": {},
            },
        )
        self.assertIs(
            (await runtime.state(SESSION_ID)).status,
            RuntimeStatus.RUNNABLE,
        )

    async def test_cancellation_writes_control_fact(self):
        async def propose(input_data: ProposalInput):
            return ControlApprovalRequest(
                "approval-1",
                "test.install",
                {"value": input_data.value},
                "安装 frozen",
                "测试风险",
            )

        control = AssistantControlPlane((_operation(propose),))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, ControlLlm(), control),
            deliver_binding(lambda _session_id, _output_id, _text: None),
        )
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await settle_session(runtime, SESSION_ID, control=control)

        async def wake(_session_id: str) -> None:
            return None

        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            SimpleNamespace(wake=wake),
            control=control,
            management=SimpleNamespace(),
        )
        message = await sessions.resolve_control(SESSION_ID, approved=False)
        self.assertEqual(message, "已取消控制操作：test.install")
        events = await journal.snapshot(SESSION_ID)
        facts = _facts(events, CONTROL_RESOLVED)
        self.assertEqual(thaw_value(facts[0].data)["approved"], False)
        self.assertTrue(facts[0].requests_decision)
        self.assertIsNone(project_pending_approval(events))

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
            deliver_binding(lambda _session_id, _output_id, _text: None),
        )
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await settle_session(runtime, SESSION_ID, control=control)

        sessions = AssistantSessions(
            runtime,
            ToolSurface(),
            SimpleNamespace(wake=_ignore_wake),
            control=control,
            management=SimpleNamespace(),
        )
        with self.assertRaisesRegex(RuntimeError, "approval execution failed"):
            await sessions.resolve_control(SESSION_ID, approved=True)

        # 执行可能已经改了世界的一半，裁决照样落成事实：重试不会再执行一次。
        events = await journal.snapshot(SESSION_ID)
        self.assertIsNone(project_pending_approval(events))
        resolved = thaw_value(_facts(events, CONTROL_RESOLVED)[0].data)
        self.assertFalse(resolved["succeeded"])
        self.assertIn("approval execution failed", resolved["message"])

    async def test_unconfirmed_approval_survives_a_fresh_control_plane(self):
        async def propose(_input: ProposalInput):
            return ControlApprovalRequest(
                "approval-1", "test.install", {}, "安装 frozen", "测试风险"
            )

        operation = _operation(propose)
        control = AssistantControlPlane((operation,))
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, ControlLlm(), control),
            deliver_binding(lambda _session_id, _output_id, _text: None),
        )
        await runtime.receive_user_message(SESSION_ID, "安装它", delivery_id="user-1")
        await settle_session(runtime, SESSION_ID, control=control)

        events = await journal.snapshot(SESSION_ID)
        restarted = AssistantControlPlane((operation,))

        self.assertEqual(project_pending_approval(events).id, "approval-1")
        self.assertEqual(restarted.schemas(SESSION_ID, events), [])

    async def test_unmatched_step_discards_staged_call(self):
        async def propose(_input: ProposalInput):
            return {"ok": True, "code": "OK"}

        control = AssistantControlPlane((_operation(propose),))
        control.stage(_frame(), PROPOSAL_NAME, {"value": "frozen"})

        self.assertIsNone(
            await control.after_committed_step(
                SESSION_ID,
                _step(trigger="trigger-2", cursor=2, basis="basis-2"),
            )
        )
        self.assertIsNone(await control.after_committed_step(SESSION_ID, _step()))
        self.assertEqual(len(control.schemas(SESSION_ID, ())), 1)

    async def test_proposal_action_must_match_operation(self):
        async def propose(_input: ProposalInput):
            return ControlApprovalRequest(
                "approval-1", "test.wrong", {}, "bad", "bad"
            )

        control = AssistantControlPlane((_operation(propose),))
        control.stage(_frame(), PROPOSAL_NAME, {"value": "frozen"})

        with self.assertRaisesRegex(ValueError, "proposal action 不匹配"):
            await control.after_committed_step(SESSION_ID, _step())
        self.assertEqual(len(control.schemas(SESSION_ID, ())), 1)

    async def test_proposal_handler_failure_becomes_a_fact(self):
        async def propose(_input: ProposalInput):
            raise RuntimeError("probe unreachable")

        control = AssistantControlPlane((_operation(propose),))
        control.stage(_frame(), PROPOSAL_NAME, {"value": "frozen"})

        outcome = await control.after_committed_step(SESSION_ID, _step())

        self.assertEqual(outcome.fact_type, CONTROL_FAILED)
        self.assertTrue(outcome.requests_decision)
        self.assertEqual(outcome.data["error"], "probe unreachable")
        self.assertIn("RuntimeError", outcome.data["traceback"])
        self.assertIsNone(project_pending_approval(()))

    async def test_proposal_conclusion_becomes_a_fact(self):
        async def propose(_input: ProposalInput):
            return {"ok": False, "code": "ALREADY_REGISTERED"}

        control = AssistantControlPlane((_operation(propose),))
        control.stage(_frame(), PROPOSAL_NAME, {"value": "frozen"})

        outcome = await control.after_committed_step(SESSION_ID, _step())

        self.assertEqual(outcome.fact_type, CONTROL_CONCLUDED)
        self.assertTrue(outcome.requests_decision)
        self.assertEqual(
            outcome.data["result"], {"ok": False, "code": "ALREADY_REGISTERED"}
        )

    async def test_proposal_conclusion_is_not_delivered(self):
        async def propose(_input: ProposalInput):
            return {"ok": False, "code": "ALREADY_REGISTERED"}

        journal = MemoryJournal()
        control = AssistantControlPlane((_operation(propose),))
        delivered: list[str] = []
        runtime = AgentRuntime(
            journal,
            _decision_maker(journal, ControlLlm(), control),
            deliver_binding(
                lambda _session_id, _output_id, text: delivered.append(text)
            ),
        )
        await runtime.receive_user_message(
            SESSION_ID, "安装它", delivery_id="user-1"
        )
        await settle_session(runtime, SESSION_ID, control=control)

        facts = [
            event.payload
            for event in await journal.snapshot(SESSION_ID)
            if isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == CONTROL_CONCLUDED
        ]
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].data["result"]["code"], "ALREADY_REGISTERED")
        self.assertNotIn("ALREADY_REGISTERED", "".join(delivered))
