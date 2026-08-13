import json
import unittest

from pydantic import BaseModel

from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tool_registry import PydanticParameters, ToolSpec
from core.tools_runtime import (
    LOAD_TOOLSET,
    RunInvocation,
    SessionCapabilitySnapshot,
    SnapshotToolsetProvider,
    ToolsetDescriptor,
)
from core.tools_runtime.progressive_toolsets import (
    LoadToolsetInput,
    ToolsetLoadingState,
    create_load_toolset_spec,
)
from core.tools_runtime.run_runtime import RunRuntime, RunStatus
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)


class WeatherInput(BaseModel):
    location: str


class FakeToolsetProvider:
    def __init__(self) -> None:
        self.requested_ids: list[str] = []

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        return (
            ToolsetDescriptor("weather", "查询当前天气和天气预报"),
            ToolsetDescriptor("database", "查询结构化数据"),
        )

    async def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        self.requested_ids.append(toolset_id)
        if toolset_id == "weather":
            async def get_weather(input_data):
                return {
                    "ok": True,
                    "code": "WEATHER_FOUND",
                    "data": {
                        "location": input_data.location,
                        "temperature": 26,
                    },
                }
            return (
                ToolSpec(
                    name="get_weather",
                    description="查询指定地点的当前天气",
                    parameters=PydanticParameters(WeatherInput),
                    handler=get_weather,
                ),
            )
        if toolset_id == "database":
            return ()
        raise AssertionError(f"unexpected toolset id: {toolset_id}")


class RecordingLLMClient:
    def __init__(self, responses, provider: FakeToolsetProvider | None = None):
        self.responses = list(responses)
        self.provider = provider
        self.requests = []

    async def chat(self, messages, model, tools=None):
        self.requests.append(
            {
                "messages": messages,
                "tools": tools or [],
                "provider_requests": tuple(self.provider.requested_ids)
                if self.provider is not None
                else (),
            }
        )
        return call_result(self.responses.pop(0))


def tool_names(request: dict) -> set[str]:
    return {
        tool["function"]["name"]
        for tool in request["tools"]
    }


class ProgressiveToolsetsTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def runner(client: RecordingLLMClient) -> RunRuntime:
        return RunRuntime(
            model_call_service(client),
            "test-model",
            PlainMode(),
            context_preparation_service(),
            **runtime_tool_dependencies(),
        )

    @staticmethod
    def conversation() -> Conversation:
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        return conversation

    async def test_toolset_is_loaded_for_the_next_round_only(self):
        provider = FakeToolsetProvider()
        client = RecordingLLMClient(
            [
                LLMResponse(
                    calls=(
                        ToolCall(
                            "load-1",
                            LOAD_TOOLSET,
                            json.dumps({"toolset_id": "weather"}),
                        ),
                    )
                ),
                LLMResponse(
                    calls=(
                        ToolCall(
                            "weather-1",
                            "get_weather",
                            json.dumps({"location": "北京"}),
                        ),
                    )
                ),
                LLMResponse(content="北京当前 26℃"),
            ],
            provider,
        )

        result = await self.runner(client).run(
            self.conversation(),
            "查询北京天气",
            invocation=RunInvocation(toolset_provider=provider),
        )

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertIn(LOAD_TOOLSET, tool_names(client.requests[0]))
        self.assertNotIn("get_weather", tool_names(client.requests[0]))
        self.assertIn("get_weather", tool_names(client.requests[1]))
        self.assertEqual(client.requests[0]["provider_requests"], ())
        self.assertEqual(client.requests[1]["provider_requests"], ("weather",))
        initial_prompt = client.requests[0]["messages"][0]["content"]
        self.assertIn("weather: 查询当前天气和天气预报", initial_prompt)
        self.assertIn("database: 查询结构化数据", initial_prompt)
        self.assertNotIn("get_weather", initial_prompt)
        self.assertEqual(
            [step.name for step in result.evidence.steps],
            [LOAD_TOOLSET, "get_weather"],
        )
        self.assertEqual(
            result.evidence.steps[0].result["data"]["tools"],
            [{
                "name": "get_weather",
                "description": "查询指定地点的当前天气",
            }],
        )

    async def test_loaded_toolset_does_not_leak_into_the_next_run(self):
        first_client = RecordingLLMClient(
            [
                LLMResponse(
                    calls=(
                        ToolCall(
                            "load-1",
                            LOAD_TOOLSET,
                            json.dumps({"toolset_id": "weather"}),
                        ),
                    )
                ),
                LLMResponse(content="已加载"),
            ]
        )
        runner = self.runner(first_client)
        invocation = RunInvocation(toolset_provider=FakeToolsetProvider())
        await runner.run(self.conversation(), "加载天气能力", invocation=invocation)

        second_client = RecordingLLMClient([LLMResponse(content="再次运行")])
        runner.model_calls = model_call_service(second_client)
        await runner.run(self.conversation(), "新的请求", invocation=invocation)

        self.assertNotIn("get_weather", tool_names(second_client.requests[0]))
        self.assertIn(LOAD_TOOLSET, tool_names(second_client.requests[0]))

    async def test_adjacent_run_keeps_receipt_and_reloads_before_calling(self):
        provider = FakeToolsetProvider()
        conversation = self.conversation()
        first_client = RecordingLLMClient([
            LLMResponse(calls=(ToolCall(
                "load-1",
                LOAD_TOOLSET,
                json.dumps({"toolset_id": "weather"}),
            ),)),
            LLMResponse(content="天气 Toolset 提供查询能力"),
        ], provider)
        runner = self.runner(first_client)
        invocation = RunInvocation(toolset_provider=provider)
        await runner.run(conversation, "天气能力是做什么的", invocation=invocation)

        second_client = RecordingLLMClient([
            LLMResponse(calls=(ToolCall(
                "load-2",
                LOAD_TOOLSET,
                json.dumps({"toolset_id": "weather"}),
            ),)),
            LLMResponse(calls=(ToolCall(
                "weather-2",
                "get_weather",
                json.dumps({"location": "北京"}, ensure_ascii=False),
            ),)),
            LLMResponse(content="北京当前 26℃"),
        ], provider)
        runner.model_calls = model_call_service(second_client)
        await runner.run(conversation, "那实际查一下北京", invocation=invocation)

        first_request = second_client.requests[0]
        context_text = json.dumps(first_request["messages"], ensure_ascii=False)
        self.assertIn("TOOLSET_LOADED", context_text)
        self.assertIn("get_weather", context_text)
        self.assertIn("历史工具结果", context_text)
        self.assertNotIn("get_weather", tool_names(first_request))
        self.assertIn("get_weather", tool_names(second_client.requests[1]))
        self.assertEqual(provider.requested_ids, ["weather", "weather"])

    async def test_loaded_toolset_specs_are_snapshotted_once_per_run(self):
        provider = FakeToolsetProvider()
        client = RecordingLLMClient(
            [
                LLMResponse(calls=(ToolCall(
                    "load-1",
                    LOAD_TOOLSET,
                    json.dumps({"toolset_id": "weather"}),
                ),)),
                LLMResponse(calls=(ToolCall(
                    "weather-1",
                    "get_weather",
                    json.dumps({"location": "北京"}),
                ),)),
                LLMResponse(content="完成"),
            ],
            provider,
        )

        await self.runner(client).run(
            self.conversation(),
            "查询天气",
            invocation=RunInvocation(toolset_provider=provider),
        )

        self.assertEqual(provider.requested_ids, ["weather"])
        self.assertEqual(
            [request["provider_requests"] for request in client.requests],
            [(), ("weather",), ("weather",)],
        )

    async def test_repeated_load_returns_same_receipt_without_rediscovery(self):
        provider = FakeToolsetProvider()
        state = ToolsetLoadingState()
        spec = create_load_toolset_spec(provider.descriptors(), state, provider)
        load_input = LoadToolsetInput(toolset_id="weather")
        first = await spec.handler(load_input)
        second = await spec.handler(load_input)

        self.assertEqual(first, second)
        self.assertEqual(provider.requested_ids, ["weather"])

    async def test_unknown_toolset_is_a_recoverable_tool_input_error(self):
        client = RecordingLLMClient(
            [
                LLMResponse(
                    calls=(
                        ToolCall(
                            "load-1",
                            LOAD_TOOLSET,
                            json.dumps({"toolset_id": "missing"}),
                        ),
                    )
                ),
                LLMResponse(content="未找到该能力"),
            ]
        )

        result = await self.runner(client).run(
            self.conversation(),
            "加载不存在的能力",
            invocation=RunInvocation(toolset_provider=FakeToolsetProvider()),
        )

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.evidence.steps[0].result["code"], "TOOLSET_NOT_FOUND")
        self.assertNotIn("get_weather", tool_names(client.requests[1]))

    async def test_composite_provider_rejects_duplicate_ids(self):
        from core.tools_runtime import CompositeToolsetProvider

        with self.assertRaises(ValueError):
            CompositeToolsetProvider(
                (FakeToolsetProvider(), FakeToolsetProvider()),
            )

    async def test_session_snapshot_admits_only_unchanged_capabilities(self):
        class MutableProvider(FakeToolsetProvider):
            def __init__(self):
                super().__init__()
                self.items = [ToolsetDescriptor("weather", "天气", 3)]

            def descriptors(self):
                return tuple(self.items)

        provider = MutableProvider()
        snapshot = SessionCapabilitySnapshot.capture(provider)
        scoped = SnapshotToolsetProvider(provider, snapshot)

        provider.items.append(ToolsetDescriptor("new", "新能力", 1))
        self.assertEqual(
            [item.id for item in scoped.descriptors()],
            ["weather"],
        )

        provider.items[0] = ToolsetDescriptor("weather", "已更新", 4)
        self.assertEqual(scoped.descriptors(), ())
        with self.assertRaisesRegex(Exception, "能力快照已过期"):
            await scoped.tool_specs("weather")

    async def test_any_catalog_change_blocks_already_loaded_tool_calls(self):
        class MutableProvider(FakeToolsetProvider):
            def __init__(self):
                super().__init__()
                self.items = [ToolsetDescriptor("weather", "天气", 1)]

            def descriptors(self):
                return tuple(self.items)

        provider = MutableProvider()
        scoped = SnapshotToolsetProvider(
            provider,
            SessionCapabilitySnapshot.capture(provider),
        )
        loaded = await scoped.tool_specs("weather")
        provider.items.append(ToolsetDescriptor("new", "新能力", 1))

        result = await loaded[0].handler(WeatherInput(location="北京"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SESSION_CAPABILITIES_STALE")

    async def test_changed_toolset_reports_stale_before_catalog_not_found(self):
        class MutableProvider(FakeToolsetProvider):
            def __init__(self):
                super().__init__()
                self.items = [ToolsetDescriptor("weather", "天气", 1)]

            def descriptors(self):
                return tuple(self.items)

        provider = MutableProvider()
        scoped = SnapshotToolsetProvider(
            provider,
            SessionCapabilitySnapshot.capture(provider),
        )
        descriptors = scoped.descriptors()
        load_spec = create_load_toolset_spec(
            descriptors,
            ToolsetLoadingState(),
            scoped,
        )
        provider.items[0] = ToolsetDescriptor("weather", "天气", 2)

        result = await load_spec.handler(LoadToolsetInput(toolset_id="weather"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "SESSION_CAPABILITIES_STALE")

    async def test_provider_token_detects_configuration_outside_visible_catalog(self):
        class TokenProvider(FakeToolsetProvider):
            def __init__(self):
                super().__init__()
                self.token = 1

            def toolset_snapshot_token(self):
                return self.token

        provider = TokenProvider()
        scoped = SnapshotToolsetProvider(
            provider,
            SessionCapabilitySnapshot.capture(provider),
        )
        provider.token = 2

        with self.assertRaisesRegex(Exception, "能力快照已过期"):
            await scoped.tool_specs("weather")


if __name__ == "__main__":
    unittest.main()
