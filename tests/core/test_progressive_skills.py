import json
import unittest
from unittest.mock import Mock

from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tools_runtime import (
    LOAD_SKILL,
    READ_SKILL_RESOURCE,
    LoadedSkill,
    SessionSkillSnapshot,
    SkillDescriptor,
    SnapshotSkillProvider,
    TurnInvocation,
)
from core.tools_runtime.progressive_skills import (
    LoadSkillInput,
    SkillBudget,
    SkillLoadingState,
    create_load_skill_spec,
)
from core.tools_runtime.turn_runtime import TurnStatus
from tests.core.environment_test_support import BoundTurnRuntime as TurnRuntime
from tests.core.environment_test_support import BoundSessionRuntime as SessionRuntime
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


class FakeSkillProvider:
    def __init__(self) -> None:
        self.items = [SkillDescriptor("pdf", "PDF workflow", 3)]
        self.loads: list[tuple[str, int]] = []
        self.reads: list[tuple[str, str]] = []

    def descriptors(self):
        return tuple(self.items)

    async def load_skill(self, skill_id, expected_revision):
        self.loads.append((skill_id, expected_revision))
        return LoadedSkill(
            name=skill_id,
            description="PDF workflow",
            revision=expected_revision,
            main_instructions="SECRET FULL PDF INSTRUCTIONS",
            skill_dir="C:/agent/skills/pdf",
        )

    async def read_resource(
        self,
        skill_id,
        expected_revision,
        relative_path,
        offset,
        limit,
    ):
        self.reads.append((skill_id, relative_path))
        return {
            "ok": True,
            "code": "SKILL_RESOURCE_READ",
            "data": {"content": "reference", "next_offset": None},
        }


class RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, model, tools=None):
        self.requests.append({"messages": messages, "tools": tools or []})
        return call_result(self.responses.pop(0))


def tool_names(request):
    return {tool["function"]["name"] for tool in request["tools"]}


class ProgressiveSkillsTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def runner(client):
        return TurnRuntime(
            model_call_service(client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

    @staticmethod
    def conversation():
        value = Conversation()
        value.set_system_prompt("system prompt")
        return value

    async def test_main_instructions_enter_next_step_not_tool_result_or_conversation(self):
        provider = FakeSkillProvider()
        client = RecordingClient([
            LLMResponse(calls=(ToolCall(
                "load-1",
                LOAD_SKILL,
                '{"skill_id":"pdf"}',
            ),)),
            LLMResponse(calls=(ToolCall(
                "read-1",
                READ_SKILL_RESOURCE,
                '{"skill_id":"pdf","relative_path":"references/guide.md"}',
            ),)),
            LLMResponse(content="done"),
        ])
        conversation = self.conversation()

        result = await self.runner(client).run(
            conversation,
            "create PDF",
            invocation=TurnInvocation(skill_provider=provider),
        )

        self.assertEqual(result.status, TurnStatus.COMPLETED)
        first_context = json.dumps(client.requests[0]["messages"])
        second_context = json.dumps(client.requests[1]["messages"])
        self.assertIn("pdf: PDF workflow", first_context)
        self.assertNotIn("SECRET FULL PDF INSTRUCTIONS", first_context)
        self.assertIn("SECRET FULL PDF INSTRUCTIONS", second_context)
        self.assertIn('directory=\\"C:/agent/skills/pdf\\"', second_context)
        self.assertIn(LOAD_SKILL, tool_names(client.requests[0]))
        self.assertIn(READ_SKILL_RESOURCE, tool_names(client.requests[0]))
        self.assertEqual(provider.loads, [("pdf", 3)])
        self.assertEqual(provider.reads, [("pdf", "references/guide.md")])
        self.assertEqual(
            [item.result["code"] for item in result.evidence.steps],
            ["SKILL_LOADED", "SKILL_RESOURCE_READ"],
        )
        self.assertNotIn(
            "SECRET FULL PDF INSTRUCTIONS",
            json.dumps(conversation.protocol_messages()),
        )
        self.assertNotIn(
            "SECRET FULL PDF INSTRUCTIONS",
            repr(result.evidence),
        )

    async def test_new_turn_keeps_receipt_but_drops_loaded_instructions(self):
        provider = FakeSkillProvider()
        conversation = self.conversation()
        first_client = RecordingClient([
            LLMResponse(calls=(ToolCall(
                "load-1",
                LOAD_SKILL,
                '{"skill_id":"pdf"}',
            ),)),
            LLMResponse(content="loaded"),
        ])
        runner = self.runner(first_client)
        invocation = TurnInvocation(skill_provider=provider)
        await runner.run(conversation, "first", invocation=invocation)

        second_client = RecordingClient([LLMResponse(content="second")])
        runner.model_calls = model_call_service(second_client)
        await runner.run(conversation, "second", invocation=invocation)
        context = json.dumps(second_client.requests[0]["messages"])

        self.assertIn("SKILL_LOADED", context)
        self.assertNotIn("SECRET FULL PDF INSTRUCTIONS", context)

    async def test_budget_failure_is_atomic_and_keeps_existing_loaded_skill(self):
        provider = FakeSkillProvider()
        existing = LoadedSkill(
            "existing",
            "existing",
            1,
            "1234",
            "C:/existing",
        )
        state = SkillLoadingState({"existing": existing})
        spec = create_load_skill_spec(
            provider.descriptors(),
            state,
            provider,
            SkillBudget(max_loaded_instruction_chars=10),
        )

        result = await spec.handler(LoadSkillInput(skill_id="pdf"))

        self.assertEqual(result["code"], "SKILL_CONTEXT_LIMIT")
        self.assertEqual(state.loaded, {"existing": existing})

    async def test_session_snapshot_ignores_new_skill_but_rejects_changed_captured_skill(self):
        provider = FakeSkillProvider()
        scoped = SnapshotSkillProvider(
            provider,
            SessionSkillSnapshot.capture(provider),
        )
        provider.items.append(SkillDescriptor("new", "new", 1))

        self.assertEqual([item.name for item in scoped.descriptors()], ["pdf"])
        loaded = await scoped.load_skill("pdf", 3)
        self.assertEqual(loaded.name, "pdf")

        provider.items[0] = SkillDescriptor("pdf", "changed", 4)
        with self.assertRaisesRegex(Exception, "Session 创建后变化"):
            await scoped.load_skill("pdf", 3)

    async def test_session_runtime_captures_enabled_skill_catalog_at_creation(self):
        provider = FakeSkillProvider()
        runtime = SessionRuntime(
            turn_runtime=Mock(),
            default_skill_provider=provider,
        )

        session = runtime.create_session("session-1", "system")
        provider.items.append(SkillDescriptor("new", "new", 1))

        self.assertEqual(
            [item.name for item in session.skill_snapshot.descriptors],
            ["pdf"],
        )


if __name__ == "__main__":
    unittest.main()
