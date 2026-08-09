from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

import tiktoken

from core.agent_application import AgentApplication
from core.context import (
    ContextBudget,
    ContextManager,
    ContextPreparationService,
    LLMContextSummaryGenerator,
    MicroCompactionConfig,
    MicroCompactionPolicy,
    ModelBudgetConfig,
    TiktokenTokenEstimator,
)
from core.model_call.client import (
    LLMClient,
    LLMContextLengthError,
    LLMTransientError,
)
from core.model_call.config import load_model_config
from core.model_call.service import ModelCallService
from core.model_call.types import InvalidLLMResponse, LLMCallResult, LLMUsage
from core.prompt import DEFAULT_AGENT_PROMPT
from core.runtime_artifacts import (
    ArtifactChunk,
    ArtifactNotFoundError,
    ArtifactOffsetOutOfRangeError,
    ArtifactRef,
    ToolResultExternalizer,
    ToolResultLimit,
)
from core.runtime_modes import PlainMode, RunMode, RuntimeModeRouter
from core.session import SessionRuntime
from core.todos import TodoMode
from core.tool_registry import BUILTIN_TOOL_REGISTRY
from core.tools_runtime.run_runtime import RunRuntime
from core.tools_runtime.tools_executor import ToolsExecutor
from tools import create_workspace_tool_specs
from tools.artifact_read import create_read_artifact_spec
from tools.powershell_runner import PowerShellCommandRunner
from tools.workspace import WorkspaceSandbox, WorkspaceSandboxes

import tools  # noqa: F401


MODEL = load_model_config().name
MODEL_CONTEXT_LIMIT = 200_000
INPUT_BUDGET_RATIO = 0.9
RECENT_PROTECTION_TOKENS = 10_000
WORKSPACE_ROOT = Path(r"D:\work\agent")
QUESTIONS = [
    "给我讲讲nanoHelper的架构",
    "我想知道它执行command cli的步骤是怎么设计的",
    "他设计的挺好的",
    "帮我检查一下，我当前环境有ripgrep，fd吗？",
    "检查一下我电脑的配置",
    "看来我的电脑还行",
]


class ContentAddressedArtifactStore:
    def __init__(self) -> None:
        self.contents: dict[str, str] = {}

    def save(self, content: str) -> ArtifactRef:
        artifact_id = "art_" + sha256(content.encode("utf-8")).hexdigest()[:32]
        self.contents[artifact_id] = content
        return ArtifactRef(artifact_id, len(content))

    def read(self, artifact_id: str, offset: int, limit: int) -> ArtifactChunk:
        if artifact_id not in self.contents:
            raise ArtifactNotFoundError(artifact_id)
        content = self.contents[artifact_id]
        if offset > len(content):
            raise ArtifactOffsetOutOfRangeError(
                f"offset={offset}, total_chars={len(content)}"
            )
        end = min(offset + limit, len(content))
        return ArtifactChunk(
            artifact_id=artifact_id,
            content=content[offset:end],
            offset=offset,
            next_offset=end if end < len(content) else None,
            total_chars=len(content),
        )


class NoDehydrationPolicy(MicroCompactionPolicy):
    def _eligible_tool_message_ids(
        self,
        records,
        *,
        max_index_exclusive: int,
    ) -> list[str]:
        return []


class FullDehydrationPolicy(MicroCompactionPolicy):
    def _recent_start_index(
        self,
        records,
        minimum_index: int,
    ) -> int:
        return len(records)


@dataclass(frozen=True)
class ModelObservation:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    result: LLMCallResult | None
    error_type: str | None = None
    error_message: str | None = None


class RecordingLLMClient:
    def __init__(self, delegate: LLMClient) -> None:
        self.delegate = delegate
        self.observations: list[ModelObservation] = []

    def chat(self, messages, model, tools=None) -> LLMCallResult:
        request_tools = tools or []
        try:
            result = self.delegate.chat(messages, model, tools)
        except (
            LLMTransientError,
            LLMContextLengthError,
            InvalidLLMResponse,
        ) as exc:
            self.observations.append(
                ModelObservation(
                    messages=deepcopy(messages),
                    tools=deepcopy(request_tools),
                    result=None,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            raise
        self.observations.append(
            ModelObservation(
                messages=deepcopy(messages),
                tools=deepcopy(request_tools),
                result=deepcopy(result),
            )
        )
        return result


class ReplayLLMClient:
    def __init__(self, recorded: list[ModelObservation]) -> None:
        self.recorded = recorded
        self.observations: list[ModelObservation] = []
        self.index = 0
        self.encoding = tiktoken.get_encoding("o200k_base")

    def chat(self, messages, model, tools=None) -> LLMCallResult:
        if self.index >= len(self.recorded):
            raise AssertionError("B 组模型调用次数超过 A 组轨迹")
        expected = self.recorded[self.index]
        self.index += 1
        request_tools = tools or []
        if expected.result is None:
            error_types = {
                "LLMTransientError": LLMTransientError,
                "LLMContextLengthError": LLMContextLengthError,
                "InvalidLLMResponse": InvalidLLMResponse,
            }
            error_type = error_types[expected.error_type]
            self.observations.append(
                ModelObservation(
                    messages=deepcopy(messages),
                    tools=deepcopy(request_tools),
                    result=None,
                    error_type=expected.error_type,
                    error_message=expected.error_message,
                )
            )
            if error_type is InvalidLLMResponse:
                raise InvalidLLMResponse(
                    "replayed_invalid_response",
                    expected.error_message or "replayed error",
                )
            raise error_type(expected.error_message or "replayed error")
        input_tokens = request_token_count(
            self.encoding,
            messages,
            request_tools,
        )
        result = LLMCallResult(
            response=deepcopy(expected.result.response),
            usage=LLMUsage(
                input_tokens=input_tokens,
                output_tokens=expected.result.usage.output_tokens,
            ),
        )
        self.observations.append(
            ModelObservation(
                messages=deepcopy(messages),
                tools=deepcopy(request_tools),
                result=deepcopy(result),
            )
        )
        return result


@dataclass(frozen=True)
class ToolObservation:
    name: str
    arguments: str
    result: dict[str, Any]


class RecordingToolsExecutor:
    def __init__(self, delegate: ToolsExecutor) -> None:
        self.delegate = delegate
        self.registry = delegate.registry
        self.observations: list[ToolObservation] = []

    def execute(self, name: str, arguments: str) -> dict[str, Any]:
        result = self.delegate.execute(name, arguments)
        self.observations.append(
            ToolObservation(name, arguments, deepcopy(result))
        )
        return result


class ReplayToolsExecutor:
    def __init__(self, registry, recorded: list[ToolObservation]) -> None:
        self.registry = registry
        self.recorded = recorded
        self.index = 0

    def execute(self, name: str, arguments: str) -> dict[str, Any]:
        if self.index >= len(self.recorded):
            raise AssertionError("B 组工具调用次数超过 A 组轨迹")
        expected = self.recorded[self.index]
        self.index += 1
        if (name, arguments) != (expected.name, expected.arguments):
            raise AssertionError(
                "A/B 工具轨迹不一致: "
                f"actual={(name, arguments)!r}, "
                f"expected={(expected.name, expected.arguments)!r}"
            )
        return deepcopy(expected.result)


def request_token_count(encoding, messages, tools_schema) -> int:
    payload = {
        "messages": messages,
        "tools": tools_schema,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return len(encoding.encode_ordinary(serialized))


def build_registry(artifact_store):
    workspaces = WorkspaceSandboxes({
        "project": WorkspaceSandbox(WORKSPACE_ROOT),
    })
    registry = BUILTIN_TOOL_REGISTRY.clone()
    runner = PowerShellCommandRunner()
    for spec in create_workspace_tool_specs(workspaces, runner):
        registry.register(spec)
    registry.register(create_read_artifact_spec(artifact_store))
    return registry


def build_application(
    llm_client,
    tools_executor,
    artifact_store,
    *,
    dehydration_strategy: str,
) -> AgentApplication:
    context_budget = ContextBudget(
        estimator=TiktokenTokenEstimator(),
        config=ModelBudgetConfig(
            context_limit=MODEL_CONTEXT_LIMIT,
            input_ratio=INPUT_BUDGET_RATIO,
        ),
    )
    model_calls = ModelCallService(llm_client, context_budget)
    context_manager = ContextManager(ToolResultLimit().max_chars)
    policy_types = {
        "full": FullDehydrationPolicy,
        "current": MicroCompactionPolicy,
        "none": NoDehydrationPolicy,
    }
    policy_type = policy_types[dehydration_strategy]
    context_preparation = ContextPreparationService(
        context_manager=context_manager,
        micro_compaction_policy=policy_type(
            context_manager=context_manager,
            context_budget=context_budget,
            config=MicroCompactionConfig(
                recent_protection_tokens=RECENT_PROTECTION_TOKENS,
            ),
            artifact_store=artifact_store,
        ),
        context_budget=context_budget,
        summary_generator=LLMContextSummaryGenerator(model_calls, MODEL),
    )
    run_runtime = RunRuntime(
        model_calls=model_calls,
        model=MODEL,
        mode_router=RuntimeModeRouter(),
        runtime_modes={
            RunMode.PLAIN: PlainMode(),
            RunMode.TODO: TodoMode(),
        },
        context_preparation=context_preparation,
        tools_executor=tools_executor,
        tool_result_externalizer=ToolResultExternalizer(
            artifact_store,
            ToolResultLimit(),
        ),
    )
    return AgentApplication(
        session_runtime=SessionRuntime(run_runtime=run_runtime),
        system_prompt=DEFAULT_AGENT_PROMPT,
    )


def request_metrics(observation: ModelObservation) -> dict[str, Any]:
    encoding = tiktoken.get_encoding("o200k_base")
    tool_messages = [
        message
        for message in observation.messages
        if message.get("role") == "tool"
    ]
    return {
        "tokens": request_token_count(
            encoding,
            observation.messages,
            observation.tools,
        ),
        "message_count": len(observation.messages),
        "tool_message_count": len(tool_messages),
        "tool_chars": sum(
            len(str(message.get("content", "")))
            for message in tool_messages
        ),
        "dehydrated_stub_count": sum(
            '"externalized":true' in str(message.get("content", "")).replace(" ", "")
            for message in tool_messages
        ),
    }


def run_group(
    application: AgentApplication,
    llm_observations: list[ModelObservation],
) -> tuple[list[dict[str, Any]], str]:
    session_id = application.create_session(f"ab-{uuid4().hex}")
    reports: list[dict[str, Any]] = []
    for turn, question in enumerate(QUESTIONS, 1):
        before = len(llm_observations)
        outcome = application.start(
            session_id,
            f"run-{turn}-{uuid4().hex}",
            question,
            max_rounds=20,
        )
        after = len(llm_observations)
        request_checkpoints = [
            checkpoint
            for checkpoint in outcome.result.checkpoints
            if checkpoint.reason == "llm_request"
        ]
        observations = llm_observations[before:after]
        if len(request_checkpoints) != len(observations):
            raise AssertionError(
                "Checkpoint 与真实模型请求数量不一致: "
                f"{len(request_checkpoints)} != {len(observations)}"
            )
        agent_requests = [
            request_metrics(observation)
            for checkpoint, observation in zip(
                request_checkpoints,
                observations,
                strict=True,
            )
            if checkpoint.data["stage"] == "agent_round"
        ]
        if not agent_requests:
            raise AssertionError(f"第 {turn} 轮没有 agent_round 请求")
        reports.append({
            "turn": turn,
            "question": question,
            "status": outcome.result.status.value,
            "answer": outcome.result.answer,
            "agent_request_count": len(agent_requests),
            "first": agent_requests[0],
            "last": agent_requests[-1],
            "peak_tokens": max(item["tokens"] for item in agent_requests),
            "cumulative_tokens": sum(item["tokens"] for item in agent_requests),
        })
        if outcome.result.status.value != "completed":
            raise RuntimeError(
                f"第 {turn} 轮未完成: {outcome.result.status.value}"
            )
    return reports, session_id


def summarize(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any]:
    comparisons = []
    for a_turn, b_turn in zip(a, b, strict=True):
        a_last = a_turn["last"]["tokens"]
        b_last = b_turn["last"]["tokens"]
        comparisons.append({
            "turn": a_turn["turn"],
            "question": a_turn["question"],
            "a_last_tokens": a_last,
            "b_last_tokens": b_last,
            "last_saved_tokens": b_last - a_last,
            "last_saved_ratio": (
                (b_last - a_last) / b_last if b_last else 0.0
            ),
            "a_cumulative_tokens": a_turn["cumulative_tokens"],
            "b_cumulative_tokens": b_turn["cumulative_tokens"],
            "cumulative_saved_tokens": (
                b_turn["cumulative_tokens"] - a_turn["cumulative_tokens"]
            ),
        })
    a_total = sum(item["cumulative_tokens"] for item in a)
    b_total = sum(item["cumulative_tokens"] for item in b)
    return {
        "turns": comparisons,
        "a_total_agent_input_tokens": a_total,
        "b_total_agent_input_tokens": b_total,
        "total_saved_tokens": b_total - a_total,
        "total_saved_ratio": (b_total - a_total) / b_total if b_total else 0.0,
        "a_final_context_tokens": a[-1]["last"]["tokens"],
        "b_final_context_tokens": b[-1]["last"]["tokens"],
        "final_saved_tokens": b[-1]["last"]["tokens"] - a[-1]["last"]["tokens"],
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not WORKSPACE_ROOT.is_dir():
        raise RuntimeError(f"实验 Workspace 不存在: {WORKSPACE_ROOT}")

    store_a = ContentAddressedArtifactStore()
    registry_a = build_registry(store_a)
    recording_llm = RecordingLLMClient(LLMClient())
    recording_tools = RecordingToolsExecutor(ToolsExecutor(registry_a))
    app_a = build_application(
        recording_llm,
        recording_tools,
        store_a,
        dehydration_strategy="current",
    )
    a_report, _ = run_group(app_a, recording_llm.observations)

    store_b = ContentAddressedArtifactStore()
    registry_b = build_registry(store_b)
    replay_llm = ReplayLLMClient(recording_llm.observations)
    replay_tools = ReplayToolsExecutor(
        registry_b,
        recording_tools.observations,
    )
    app_b = build_application(
        replay_llm,
        replay_tools,
        store_b,
        dehydration_strategy="none",
    )
    b_report, _ = run_group(app_b, replay_llm.observations)

    if replay_llm.index != len(recording_llm.observations):
        raise AssertionError("B 组未完整消费 A 组模型轨迹")
    if replay_tools.index != len(recording_tools.observations):
        raise AssertionError("B 组未完整消费 A 组工具轨迹")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "experiment": "Level 1 dehydration A/B controlled replay",
        "controls": {
            "model": MODEL,
            "questions": QUESTIONS,
            "workspace_root": str(WORKSPACE_ROOT),
            "context_limit": MODEL_CONTEXT_LIMIT,
            "input_budget_ratio": INPUT_BUDGET_RATIO,
            "recent_protection_tokens": RECENT_PROTECTION_TOKENS,
            "runtime_mode_and_model_responses": "A recorded, B replayed exactly",
            "tool_calls_and_results": "A recorded, B replayed exactly",
            "tokenizer": "o200k_base over canonical {messages, tools} JSON",
            "only_difference": (
                "A uses MicroCompactionPolicy; B overrides only "
                "_eligible_tool_message_ids to return []"
            ),
        },
        "a_with_dehydration": a_report,
        "b_without_dehydration": b_report,
        "comparison": summarize(a_report, b_report),
    }
    report_path = (
        Path(__file__).resolve().parents[2]
        / "logs"
        / f"dehydration_ab_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "report_path": str(report_path),
        "comparison": report["comparison"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
