import json
import unittest

from pydantic import BaseModel

from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tool_registry import ToolSpec
from core.tools_runtime import (
    LOAD_TOOLSET,
    RunInvocation,
    ToolsetDescriptor,
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

    def tool_specs(self, toolset_id: str) -> tuple[ToolSpec, ...]:
        self.requested_ids.append(toolset_id)
        if toolset_id == "weather":
            return (
                ToolSpec(
                    name="get_weather",
                    description="查询指定地点的当前天气",
                    input_model=WeatherInput,
                    handler=lambda input_data: {
                        "ok": True,
                        "code": "WEATHER_FOUND",
                        "data": {
                            "location": input_data.location,
                            "temperature": 26,
                        },
                    },
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

    def chat(self, messages, model, tools=None):
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


class ProgressiveToolsetsTest(unittest.TestCase):
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

    def test_toolset_is_loaded_for_the_next_round_only(self):
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

        result = self.runner(client).run(
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

    def test_loaded_toolset_does_not_leak_into_the_next_run(self):
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
        runner.run(self.conversation(), "加载天气能力", invocation=invocation)

        second_client = RecordingLLMClient([LLMResponse(content="再次运行")])
        runner.model_calls = model_call_service(second_client)
        runner.run(self.conversation(), "新的请求", invocation=invocation)

        self.assertNotIn("get_weather", tool_names(second_client.requests[0]))
        self.assertIn(LOAD_TOOLSET, tool_names(second_client.requests[0]))

    def test_unknown_toolset_is_a_recoverable_tool_input_error(self):
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

        result = self.runner(client).run(
            self.conversation(),
            "加载不存在的能力",
            invocation=RunInvocation(toolset_provider=FakeToolsetProvider()),
        )

        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.evidence.steps[0].result["code"], "TOOLSET_NOT_FOUND")
        self.assertNotIn("get_weather", tool_names(client.requests[1]))


if __name__ == "__main__":
    unittest.main()
