import json
import unittest
from unittest.mock import AsyncMock, Mock

from pydantic import BaseModel

from core.agent_application import AgentApplication
from core.approval import (
    ApprovalActionRegistry,
    ApprovalExecution,
    ApprovalRequest,
)
from core.context import ContextState
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.session.state import SessionStatus
from core.tool_registry import PydanticParameters, ToolSpec
from core.tools_runtime.turn_evidence import TurnEvidence
from core.tools_runtime.turn_runtime import TurnStatus
from tests.core.environment_test_support import (
    BoundSessionRuntime as SessionRuntime,
    BoundTurnRuntime as TurnRuntime,
)
from core.tools_runtime.turn_types import TurnResult
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


class ProposalInput(BaseModel):
    value: str


class RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, model, tools=None):
        return call_result(self.responses.pop(0))


def conversation() -> Conversation:
    value = Conversation()
    value.set_system_prompt("system")
    return value


class ApprovalRuntimeTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def runner(client, specs):
        dependencies = runtime_tool_dependencies()
        for spec in specs:
            dependencies["tools_executor"].registry.register(spec)
        return TurnRuntime(
            model_call_service(client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **dependencies,
        )

    async def test_control_tool_blocks_turn_and_records_frozen_request(self):
        request = ApprovalRequest(
            id="approval-1",
            action="demo.install",
            payload={"value": "frozen"},
            summary="安装 demo",
            risk="启动外部进程",
        )

        async def propose(_input):
            return request

        spec = ToolSpec(
            name="propose_install",
            description="proposal",
            parameters=PydanticParameters(ProposalInput),
            handler=propose,
            control_boundary=True,
            exclusive_batch=True,
        )
        client = RecordingClient([
            LLMResponse(calls=(ToolCall(
                "call-1",
                "propose_install",
                json.dumps({"value": "frozen"}),
            ),)),
        ])
        history = conversation()

        result = await self.runner(client, (spec,)).run(
            history,
            "安装 demo",
        )

        self.assertEqual(result.status, TurnStatus.BLOCKED)
        self.assertIs(result.approval_request, request)
        self.assertIs(history.get_approval_request("approval-1"), request)
        self.assertEqual(result.final_reason, "approval_required")
        self.assertIn("输入 yes 确认", result.answer)
        self.assertEqual(
            result.evidence.steps[0].result["code"],
            "APPROVAL_REQUIRED",
        )

    async def test_control_tool_in_mixed_batch_rejects_entire_batch(self):
        executions = []

        async def propose(_input):
            executions.append("proposal")
            return ApprovalRequest(
                "approval-1",
                "demo.install",
                {},
                "安装 demo",
                "风险",
            )

        async def mutate(_input):
            executions.append("mutation")
            return {"ok": True, "code": "MUTATED"}

        proposal = ToolSpec(
            "propose_install",
            "proposal",
            PydanticParameters(ProposalInput),
            propose,
            control_boundary=True,
            exclusive_batch=True,
        )
        mutation = ToolSpec(
            "mutate",
            "mutation",
            PydanticParameters(ProposalInput),
            mutate,
        )
        client = RecordingClient([
            LLMResponse(calls=(
                ToolCall("call-1", "propose_install", '{"value":"x"}'),
                ToolCall("call-2", "mutate", '{"value":"x"}'),
            )),
            LLMResponse(content="已按要求重试"),
        ])

        result = await self.runner(client, (proposal, mutation)).run(
            conversation(),
            "安装 demo",
        )

        self.assertEqual(result.status, TurnStatus.COMPLETED)
        self.assertEqual(executions, [])
        self.assertEqual(
            [item.result["code"] for item in result.evidence.steps],
            [
                "EXCLUSIVE_TOOL_REQUIRES_EXCLUSIVE_BATCH",
                "EXCLUSIVE_TOOL_REQUIRES_EXCLUSIVE_BATCH",
            ],
        )


class DemoApprovalHandler:
    action = "demo.install"

    def __init__(self):
        self.payloads = []

    async def execute(self, payload):
        self.payloads.append(dict(payload))
        return ApprovalExecution(True, "安装成功", {"revision": 2})


class ApprovalApplicationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = SessionRuntime(turn_runtime=Mock())
        self.handler = DemoApprovalHandler()
        actions = ApprovalActionRegistry()
        actions.register(self.handler)
        self.application = AgentApplication(
            self.runtime,
            "system",
            approval_actions=actions,
        )
        await self.application.__aenter__()
        self.application.create_session("session-1")
        self.session = self.runtime.get_session("session-1")
        self.request = ApprovalRequest(
            "approval-1",
            "demo.install",
            {"value": "frozen"},
            "安装 demo",
            "风险",
        )
        self.session.conversation.record_approval_request(self.request)
        self.session.pending_approval_id = self.request.id
        self.session.status = SessionStatus.BLOCKED

    async def asyncTearDown(self):
        await self.application.close()

    async def test_yes_executes_frozen_action_exactly_once(self):
        resolution = await self.application.resolve_approval(
            "session-1",
            "yes",
        )

        self.assertEqual(resolution.decision, "approved")
        self.assertTrue(resolution.execution.succeeded)
        self.assertEqual(self.handler.payloads, [{"value": "frozen"}])
        self.assertIsNone(self.session.pending_approval_id)
        self.assertIn(
            "approval-1",
            self.session.conversation.approval_resolutions,
        )
        with self.assertRaises(ValueError):
            await self.application.resolve_approval("session-1", "yes")

        messages = self.session.conversation.protocol_messages()
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user"],
        )
        self.assertEqual(messages[-1]["content"], "yes")
        self.assertFalse(
            any("安装成功" in str(message.get("content")) for message in messages)
        )

    async def test_no_records_rejection_without_executing(self):
        resolution = await self.application.resolve_approval(
            "session-1",
            "no",
        )

        self.assertEqual(resolution.decision, "rejected")
        self.assertEqual(self.handler.payloads, [])
        self.assertIsNone(self.session.pending_approval_id)

    async def test_confirmation_is_exact(self):
        for value in ("YES", "y", " yes ", "好的"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    await self.application.resolve_approval(
                        "session-1",
                        value,
                    )
        self.assertEqual(self.session.pending_approval_id, "approval-1")


if __name__ == "__main__":
    unittest.main()
