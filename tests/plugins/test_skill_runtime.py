import json
import tempfile
import unittest
from pathlib import Path

from core.agent_workspace import AgentWorkspace
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tools_runtime.turn_invocation import TurnInvocation
from core.tools_runtime.turn_runtime import TurnStatus
from plugins.skills.application import SkillApplicationService
from plugins.skills.runtime import (
    LOAD_SKILL,
    READ_SKILL_RESOURCE,
    LoadSkillInput,
    ReadSkillResourceInput,
)
from tests.core.environment_test_support import BoundTurnRuntime as TurnRuntime
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)
from tests.plugins.test_skill_package import write_skill


class RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, model, tools=None):
        self.requests.append({"messages": messages, "tools": tools or []})
        return call_result(self.responses.pop(0))


def tool_by_name(tools, name):
    return next(item for item in tools if item["function"]["name"] == name)


class SkillRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = AgentWorkspace(root / ".helperme")
        self.workspace.initialize()
        self.source = root / "source"
        write_skill(
            self.source,
            name="demo",
            description="Demo workflow",
            body="\nFollow the demo workflow.\n",
        )
        reference = self.source / "references" / "guide.md"
        reference.parent.mkdir()
        reference.write_text("abcdefghij", encoding="utf-8")
        self.service = SkillApplicationService(self.workspace)
        await self.service.install_local(self.source)
        self.record = await self.service.set_enabled("demo", True)
        self.runtime = self.service.runtime_capability

    async def asyncTearDown(self):
        self.temporary.cleanup()

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

    async def test_runtime_is_two_plain_tool_specs_and_load_returns_content(self):
        specs = {spec.name: spec for spec in self.runtime.tool_specs()}

        self.assertEqual(set(specs), {LOAD_SKILL, READ_SKILL_RESOURCE})
        self.assertIn("demo: Demo workflow", specs[LOAD_SKILL].description)
        self.assertTrue(specs[LOAD_SKILL].exclusive_batch)

        result = await specs[LOAD_SKILL].handler(LoadSkillInput(skill_id="demo"))

        self.assertEqual(result["code"], "SKILL_LOADED")
        self.assertEqual(result["data"]["skill_id"], "demo")
        self.assertEqual(result["data"]["revision"], self.record.revision)
        self.assertEqual(
            result["data"]["content"],
            "\nFollow the demo workflow.\n",
        )

    async def test_resource_read_requires_no_runtime_loaded_state(self):
        specs = {spec.name: spec for spec in self.runtime.tool_specs()}

        result = await specs[READ_SKILL_RESOURCE].handler(
            ReadSkillResourceInput(
                skill_id="demo",
                relative_path="references/guide.md",
                offset=2,
                limit=4,
            )
        )

        self.assertEqual(result["code"], "SKILL_RESOURCE_READ")
        self.assertEqual(result["data"]["content"], "cdef")

    async def test_turn_specs_refresh_catalog_but_old_closure_rejects_change(self):
        old_specs = {spec.name: spec for spec in self.runtime.tool_specs()}
        await self.service.set_enabled("demo", False)

        stale = await old_specs[LOAD_SKILL].handler(
            LoadSkillInput(skill_id="demo")
        )

        self.assertEqual(stale["code"], "SKILL_CATALOG_STALE")
        self.assertEqual(self.runtime.tool_specs(), [])

    async def test_loaded_instruction_persists_in_conversation_across_turns(self):
        first_client = RecordingClient([
            LLMResponse(calls=(ToolCall(
                "load-1",
                LOAD_SKILL,
                '{"skill_id":"demo"}',
            ),)),
            LLMResponse(content="请确认是否执行"),
        ])
        conversation = self.conversation()
        invocation = TurnInvocation(capabilities=(self.runtime,))
        runner = self.runner(first_client)

        first = await runner.run(
            conversation,
            "选择合适 Skill，先不要执行",
            invocation=invocation,
        )
        self.assertEqual(first.status, TurnStatus.COMPLETED)

        second_client = RecordingClient([LLMResponse(content="开始执行")])
        runner.model_calls = model_call_service(second_client)
        await runner.run(
            conversation,
            "确认，执行吧",
            invocation=invocation,
        )
        context = json.dumps(second_client.requests[0]["messages"])
        loaded_result = json.loads(next(
            item["content"]
            for item in second_client.requests[0]["messages"]
            if item["role"] == "tool"
        ))

        self.assertIn("Follow the demo workflow", context)
        self.assertEqual(
            loaded_result["data"]["revision"],
            self.record.revision,
        )

    async def test_each_turn_calls_capability_again_for_current_catalog(self):
        first_client = RecordingClient([LLMResponse(content="first")])
        conversation = self.conversation()
        invocation = TurnInvocation(capabilities=(self.runtime,))
        runner = self.runner(first_client)
        await runner.run(conversation, "first", invocation=invocation)

        source = Path(self.temporary.name) / "second-source"
        write_skill(
            source,
            name="second",
            description="Second workflow",
            body="\nSecond instructions.\n",
        )
        await self.service.install_local(source)
        await self.service.set_enabled("second", True)

        second_client = RecordingClient([LLMResponse(content="second")])
        runner.model_calls = model_call_service(second_client)
        await runner.run(conversation, "second", invocation=invocation)

        first_tool = tool_by_name(first_client.requests[0]["tools"], LOAD_SKILL)
        second_tool = tool_by_name(second_client.requests[0]["tools"], LOAD_SKILL)
        self.assertNotIn("second: Second workflow", first_tool["function"]["description"])
        self.assertIn("second: Second workflow", second_tool["function"]["description"])

    async def test_large_instruction_uses_generic_tool_result_artifact(self):
        source = Path(self.temporary.name) / "large-source"
        write_skill(
            source,
            name="large",
            description="Large workflow",
            body="BEGIN\n" + ("x" * 17_000) + "\nEND\n",
        )
        await self.service.install_local(source)
        await self.service.set_enabled("large", True)
        client = RecordingClient([
            LLMResponse(calls=(ToolCall(
                "load-large",
                LOAD_SKILL,
                '{"skill_id":"large"}',
            ),)),
            LLMResponse(content="loaded"),
        ])

        result = await self.runner(client).run(
            self.conversation(),
            "load large",
            invocation=TurnInvocation(capabilities=(self.runtime,)),
        )
        tool_result = json.loads(next(
            item["content"]
            for item in client.requests[1]["messages"]
            if item["role"] == "tool"
        ))

        self.assertEqual(result.status, TurnStatus.COMPLETED)
        self.assertEqual(tool_result["code"], "SKILL_LOADED")
        self.assertTrue(tool_result["data"]["externalized"])
        self.assertIn("read_artifact", tool_result["hint"])


if __name__ == "__main__":
    unittest.main()
