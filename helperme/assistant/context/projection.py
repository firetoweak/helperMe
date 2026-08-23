from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json

from helperme.assistant.artifacts import (
    ArtifactGateway,
    ArtifactStore,
    MemoryArtifactGateway,
    is_valid_artifact_id,
)
from helperme.assistant.context.budget import (
    BudgetAssessment,
    InputBudget,
    TiktokenEstimator,
    TokenEstimator,
)
from helperme.assistant.delivery import DELIVER_TOOL_NAME
from helperme.assistant.context.prompt import DEFAULT_ASSISTANT_PROMPT
from helperme.assistant.completion.criteria import judgment_from_fact
from helperme.runtime.events import (
    CommandOutcomeReceived,
    Event,
    StepCommitted,
    UserInterruptReceived,
    UserMessageReceived,
)
from helperme.runtime.model import (
    CancelTool,
    CommandOutcome,
    InvokeTool,
    OutcomeStatus,
)


PROJECTOR_VERSION = 1
DEFAULT_RECENT_PROTECTION_TOKENS = 10_000
DEFAULT_SIZE_EXTERNALIZE_CHARS = 16_000
DEFAULT_PREVIEW_CHARS = 1_200


@dataclass(frozen=True, slots=True)
class ModelContextSettings:
    recent_protection_tokens: int = DEFAULT_RECENT_PROTECTION_TOKENS
    size_externalize_chars: int = DEFAULT_SIZE_EXTERNALIZE_CHARS
    preview_chars: int = DEFAULT_PREVIEW_CHARS
    context_limit: int = 200_000
    input_budget_ratio: float = 0.75

    def __post_init__(self) -> None:
        if (
            type(self.recent_protection_tokens) is not int
            or self.recent_protection_tokens <= 0
        ):
            raise ValueError("recent_protection_tokens 必须大于 0")
        if (
            type(self.size_externalize_chars) is not int
            or self.size_externalize_chars <= 0
        ):
            raise ValueError("size_externalize_chars 必须大于 0")
        if (
            type(self.preview_chars) is not int
            or not 0 <= self.preview_chars < self.size_externalize_chars
        ):
            raise ValueError("preview_chars 必须大于等于 0 且小于 size 阈值")
        if type(self.context_limit) is not int or self.context_limit <= 0:
            raise ValueError("context_limit 必须大于 0")
        if (
            type(self.input_budget_ratio) is not float
            or not 0 < self.input_budget_ratio < 1
        ):
            raise ValueError("input_budget_ratio 必须在 0 和 1 之间")


class ModelContextBudgetExceeded(ValueError):
    def __init__(self, assessment: BudgetAssessment) -> None:
        super().__init__(
            "模型输入估算 "
            f"{assessment.estimated_input_tokens} 超过预算 "
            f"{assessment.input_budget_tokens}"
        )
        self.assessment = assessment


@dataclass(frozen=True, slots=True)
class PreparedModelContext:
    messages: list[dict[str, object]]
    assessment: BudgetAssessment
    protection_start_index: int
    size_externalized_command_ids: tuple[str, ...]
    age_dehydrated_command_ids: tuple[str, ...]
    projector_version: int = PROJECTOR_VERSION


@dataclass(frozen=True, slots=True)
class _Projected:
    message: dict[str, object]
    kind: str
    command_id: str | None = None


def jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    return value


def outcome_text(outcome: CommandOutcome) -> str:
    return json.dumps(
        {
            "status": outcome.status.value,
            "value": jsonable(outcome.value),
            "error_type": outcome.error_type,
            "error_message": outcome.error_message,
        },
        ensure_ascii=False,
    )


def project_chat_messages(
    events: tuple[Event, ...],
    visible_event_ids: tuple[str, ...],
    system_prompt: str = DEFAULT_ASSISTANT_PROMPT,
) -> list[dict[str, object]]:
    """把冻结可见 Event 译成模型协议消息，不脱水、不截断。"""
    return [
        item.message
        for item in _translate_visible_events(
            events,
            visible_event_ids,
            system_prompt,
        )
    ]


def _translate_visible_events(
    events: tuple[Event, ...],
    visible_event_ids: tuple[str, ...],
    system_prompt: str,
) -> list[_Projected]:
    visible = set(visible_event_ids)
    items: list[_Projected] = [
        _Projected(
            {"role": "system", "content": system_prompt},
            "system",
        ),
    ]
    commands: dict[str, InvokeTool | CancelTool] = {}
    command_ranks: dict[str, tuple[int, int]] = {}
    for event in events:
        if event.event_id not in visible:
            continue
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            items.append(_Projected(
                {"role": "user", "content": payload.content},
                "user",
            ))
            continue
        if isinstance(payload, UserInterruptReceived):
            reason = payload.reason or "interrupted"
            items.append(_Projected(
                {"role": "user", "content": f"[interrupt] {reason}"},
                "interrupt",
            ))
            continue
        judgment = judgment_from_fact(payload)
        if judgment is not None:
            items.append(_Projected(
                {
                    "role": "user",
                    "content": (
                        f"[judge {judgment.verdict.value}] {judgment.summary}"
                    ),
                },
                "judgment",
            ))
            continue
        if isinstance(payload, StepCommitted):
            shown: list[dict[str, object]] = []
            for command_index, command in enumerate(payload.step.commands):
                effect = command.effect
                commands[command.command_id] = effect
                command_ranks[command.command_id] = (
                    event.sequence,
                    command_index,
                )
                if not isinstance(effect, InvokeTool):
                    continue
                if effect.name == DELIVER_TOOL_NAME:
                    continue
                shown.append({
                    "id": command.command_id,
                    "type": "function",
                    "function": {
                        "name": effect.name,
                        "arguments": json.dumps(
                            dict(effect.arguments),
                            ensure_ascii=False,
                        ),
                    },
                })
            content = payload.step.decision.content
            if not content and not shown:
                continue
            message: dict[str, object] = {
                "role": "assistant",
                "content": content or None,
            }
            if shown:
                message["tool_calls"] = shown
            items.append(_Projected(message, "assistant"))
            continue
        if isinstance(payload, CommandOutcomeReceived):
            effect = commands[payload.command_id]
            if not isinstance(effect, InvokeTool):
                continue
            if effect.name == DELIVER_TOOL_NAME:
                continue
            items.append(_Projected(
                {
                    "role": "tool",
                    "tool_call_id": payload.command_id,
                    "content": outcome_text(payload.outcome),
                },
                "tool",
                payload.command_id,
            ))
    return _canonicalize_tool_result_runs(items, command_ranks)


def _canonicalize_tool_result_runs(
    items: list[_Projected],
    command_ranks: Mapping[str, tuple[int, int]],
) -> list[_Projected]:
    """稳定模型序列化；不改变 Journal 的真实 Outcome 到达顺序。"""

    start = 0
    while start < len(items):
        if items[start].kind != "tool":
            start += 1
            continue
        end = start + 1
        while end < len(items) and items[end].kind == "tool":
            end += 1

        def rank(item: _Projected) -> tuple[int, int]:
            if item.command_id is None:
                raise ValueError("projected tool message lacks command id")
            return command_ranks[item.command_id]

        items[start:end] = sorted(items[start:end], key=rank)
        start = end
    return items


def parse_tool_result_meta(content: object) -> tuple[bool, str | None]:
    payload: object = json.loads(content) if isinstance(content, str) else content
    if not isinstance(payload, dict):
        return False, None
    data = payload.get("data")
    if not isinstance(data, dict):
        return False, None
    artifact_id = data.get("artifact_id")
    externalized = data.get("externalized") is True
    if externalized and is_valid_artifact_id(artifact_id):
        return True, artifact_id
    return False, None


def _content_char_length(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(jsonable(content), ensure_ascii=False))


def _artifact_id_from_content(content: object) -> str | None:
    _, artifact_id = parse_tool_result_meta(content)
    if artifact_id is not None:
        return artifact_id
    payload: object = (
        json.loads(content) if isinstance(content, str) else content
    )
    if not isinstance(payload, dict):
        return None
    value = payload.get("value")
    if isinstance(value, dict):
        nested = value.get("artifact_id")
        if value.get("externalized") is True and is_valid_artifact_id(nested):
            return nested
        _, nested_id = parse_tool_result_meta(value)
        if nested_id is not None:
            return nested_id
    direct = payload.get("artifact_id")
    if payload.get("externalized") is True and is_valid_artifact_id(direct):
        return direct
    return None


def _stub_content(
    size_chars: int,
    artifact_id: str,
    preview: str = "",
) -> str:
    stub = {
        "ok": True,
        "code": "OK",
        "data": {
            "externalized": True,
            "artifact_id": artifact_id,
            "size_chars": size_chars,
            "preview": preview,
        },
        "error": None,
        "hint": "需要更多内容时调用 read_artifact 分页读取。",
    }
    return json.dumps(stub, ensure_ascii=False, separators=(",", ":"))


def externalize_payload(
    payload: object,
    store: ArtifactStore,
    *,
    max_chars: int,
    preview_chars: int,
) -> tuple[object, str | None]:
    """过大的工具返回值立刻外置；未超限则原样返回。"""
    encoded = json.dumps(jsonable(payload), ensure_ascii=False)
    if len(encoded) <= max_chars:
        return payload, None
    artifact = store.save(encoded)
    return (
        {
            "externalized": True,
            "artifact_id": artifact.artifact_id,
            "size_chars": artifact.size_chars,
            "preview": encoded[:preview_chars],
        },
        artifact.artifact_id,
    )


class ModelContextProjector:
    """产品层 Model Context：保护窗 + 体积外置 + Level 1 脱水 + 预算。

    Journal 事实不变。command_id → artifact_id 只活在投影缓存里。
    """

    def __init__(
        self,
        gateway: ArtifactGateway | None = None,
        budget: InputBudget | None = None,
        settings: ModelContextSettings | None = None,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self._gateway = (
            MemoryArtifactGateway() if gateway is None else gateway
        )
        self._settings = (
            ModelContextSettings() if settings is None else settings
        )
        self._budget = (
            InputBudget(
                TiktokenEstimator() if estimator is None else estimator,
                context_limit=self._settings.context_limit,
                input_ratio=self._settings.input_budget_ratio,
            )
            if budget is None
            else budget
        )
        self._index: dict[tuple[str, str], str] = {}

    @property
    def budget(self) -> InputBudget:
        return self._budget

    @property
    def gateway(self) -> ArtifactGateway:
        return self._gateway

    @property
    def settings(self) -> ModelContextSettings:
        return self._settings

    def prepare(
        self,
        events: tuple[Event, ...],
        visible_event_ids: tuple[str, ...],
        stream_id: str,
        system_prompt: str = DEFAULT_ASSISTANT_PROMPT,
        tools: list[dict[str, object]] | None = None,
    ) -> PreparedModelContext:
        items = [
            _Projected(deepcopy(item.message), item.kind, item.command_id)
            for item in _translate_visible_events(
                events,
                visible_event_ids,
                system_prompt,
            )
        ]
        store = self._gateway.for_stream(stream_id)
        size_ids = self._externalize_oversized(items, stream_id, store)
        protection_start = self._protection_start(items)
        age_ids = self._dehydrate_eligible(
            items,
            stream_id,
            store,
            protection_start,
        )
        messages = [item.message for item in items]
        assessment = self._budget.assess(
            messages,
            [] if tools is None else tools,
        )
        if not assessment.allowed:
            raise ModelContextBudgetExceeded(assessment)
        return PreparedModelContext(
            messages=messages,
            assessment=assessment,
            protection_start_index=protection_start,
            size_externalized_command_ids=tuple(size_ids),
            age_dehydrated_command_ids=tuple(age_ids),
        )

    def _externalize_oversized(
        self,
        items: list[_Projected],
        stream_id: str,
        store: ArtifactStore,
    ) -> list[str]:
        changed: list[str] = []
        for item in items:
            if item.kind != "tool":
                continue
            if item.command_id is None:
                raise ValueError("projected tool message lacks command id")
            content = item.message["content"]
            if _artifact_id_from_content(content) is not None:
                continue
            if _content_char_length(content) <= self._settings.size_externalize_chars:
                continue
            original = content if isinstance(content, str) else json.dumps(
                jsonable(content),
                ensure_ascii=False,
            )
            artifact_id = self._save(
                stream_id,
                item.command_id,
                original,
                store,
            )
            item.message["content"] = _stub_content(
                len(original),
                artifact_id,
                original[: self._settings.preview_chars],
            )
            changed.append(item.command_id)
        return changed

    def _dehydrate_eligible(
        self,
        items: list[_Projected],
        stream_id: str,
        store: ArtifactStore,
        protection_start: int,
    ) -> list[str]:
        changed: list[str] = []
        payloads = [item.message for item in items]
        index = 0
        while index < len(payloads):
            message = payloads[index]
            if (
                index >= protection_start
                or items[index].kind != "assistant"
                or "tool_calls" not in message
            ):
                index += 1
                continue
            calls = message["tool_calls"]
            if type(calls) is not list or not calls:
                raise TypeError("projected assistant tool_calls must be a list")

            result_end = index + 1
            while (
                result_end < len(payloads)
                and items[result_end].kind == "tool"
            ):
                result_end += 1

            results = payloads[index + 1 : result_end]
            call_ids = [call["id"] for call in calls]
            result_ids = [result["tool_call_id"] for result in results]
            batch_complete = (
                len(result_ids) == len(call_ids)
                and set(result_ids) == set(call_ids)
                and result_end <= protection_start
            )
            if not batch_complete:
                index += 1
                continue

            consumed = any(
                items[later].kind == "assistant"
                for later in range(result_end, protection_start)
            )
            succeeded = all(_tool_succeeded(result) for result in results)
            if consumed and succeeded:
                for tool_index in range(index + 1, result_end):
                    item = items[tool_index]
                    if item.command_id is None:
                        raise ValueError(
                            "projected tool message lacks command id"
                        )
                    if _artifact_id_from_content(item.message["content"]):
                        continue
                    original = item.message["content"]
                    if not isinstance(original, str):
                        original = json.dumps(
                            jsonable(original),
                            ensure_ascii=False,
                        )
                    artifact_id = self._save(
                        stream_id,
                        item.command_id,
                        original,
                        store,
                    )
                    item.message["content"] = _stub_content(
                        len(original),
                        artifact_id,
                    )
                    changed.append(item.command_id)
            index = result_end
        return changed

    def _protection_start(self, items: list[_Projected]) -> int:
        last_user = 0
        for index, item in enumerate(items):
            if item.kind == "user":
                last_user = index
        start = last_user
        while start > 1:
            recent = [item.message for item in items[start:]]
            tokens = self._budget.estimator.estimate(recent, [])
            if tokens >= self._settings.recent_protection_tokens:
                break
            start -= 1
        return start

    def _save(
        self,
        stream_id: str,
        command_id: str,
        content: str,
        store: ArtifactStore,
    ) -> str:
        key = (stream_id, command_id)
        existing = self._index.get(key)
        if existing is not None:
            return existing
        artifact_id = store.save(content).artifact_id
        self._index[key] = artifact_id
        return artifact_id


def _tool_succeeded(message: Mapping[str, object]) -> bool:
    content = message["content"]
    if not isinstance(content, str):
        raise TypeError("projected tool content must be str")
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise TypeError("projected tool content must be a JSON object")
    if "status" in payload:
        return OutcomeStatus(payload["status"]) is OutcomeStatus.SUCCEEDED
    if "ok" in payload:
        if type(payload["ok"]) is not bool:
            raise TypeError("projected tool ok must be bool")
        return payload["ok"]
    raise ValueError("projected tool content has no status")
