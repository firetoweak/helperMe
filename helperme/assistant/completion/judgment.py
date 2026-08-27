"""隔离 Judge 与 Host 侧收口门。不进入 Runtime 内核，也不恢复 Goal Loop。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from helperme.assistant.artifacts import (
    ArtifactNotFoundError,
    ArtifactOffsetOutOfRangeError,
    ArtifactGateway,
    is_valid_artifact_id,
)
from helperme.assistant.completion.criteria import (
    CriteriaCommitted,
    JudgmentCommitted,
    JudgmentVerdict,
    MUTATING_TOOLS,
    classify_user_intent,
    collect_facts,
    criteria_after_intent,
    current_criteria,
    format_facts_for_judge,
    judgment_for_step,
    criteria_fact,
    judgment_fact,
    latest_complete_step_id,
    next_criteria_from_facts,
)
from helperme.assistant.completion.prompt import JUDGE_PROMPT
from helperme.llm.api import LLMApi
from helperme.runtime import AgentRuntime
from helperme.runtime.model import LifecycleIntent


JUDGE_TOOLS = frozenset({
    "read_file",
    "glob",
    "grep",
    "get_changes",
    "execute_command",
    "read_artifact",
})
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class CompletionGate(str, Enum):
    FINALIZE = "finalize"
    PAUSE = "pause"


class JudgeFn(Protocol):
    async def __call__(
        self,
        events: tuple,
        criteria: CriteriaCommitted,
        step_id: str,
    ) -> tuple[JudgmentVerdict, str]:
        ...


class ToolRunner(Protocol):
    async def execute(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> object:
        ...


class ScriptedJudge:
    def __init__(self, verdicts: Sequence[tuple[JudgmentVerdict, str]]) -> None:
        self._verdicts = list(verdicts)

    async def __call__(
        self,
        _events: tuple,
        _criteria: CriteriaCommitted,
        _step_id: str,
    ) -> tuple[JudgmentVerdict, str]:
        if not self._verdicts:
            raise AssertionError("scripted judge exhausted")
        return self._verdicts.pop(0)


def parse_judgment(content: str) -> tuple[JudgmentVerdict, str] | None:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    match = _JSON_OBJECT.search(text)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if type(data) is not dict:
        return None
    raw = data.get("verdict")
    summary = data.get("summary") or data.get("reason")
    if type(raw) is not str or type(summary) is not str or not summary.strip():
        return None
    try:
        return JudgmentVerdict(raw), summary.strip()
    except ValueError:
        return None


class IsolatedJudge:
    def __init__(
        self,
        llm: LLMApi,
        model: str,
        tool_schemas: Sequence[dict[str, object]],
        runner: ToolRunner,
        *,
        max_rounds: int = 8,
    ) -> None:
        self._llm = llm
        self._model = model
        self._tool_schemas = [
            schema
            for schema in tool_schemas
            if _schema_name(schema) in JUDGE_TOOLS
        ]
        self._runner = runner
        self._max_rounds = max_rounds

    async def __call__(
        self,
        events: tuple,
        criteria: CriteriaCommitted,
        step_id: str,
    ) -> tuple[JudgmentVerdict, str]:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": JUDGE_PROMPT},
            {
                "role": "user",
                "content": _judge_user_prompt(events, criteria, step_id),
            },
        ]
        for _ in range(self._max_rounds):
            result = await self._llm.chat(
                messages,
                self._model,
                tools=self._tool_schemas or None,
            )
            response = result.response
            if response.calls:
                messages.append(_assistant_tool_message(response))
                for call in response.calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": await self._run_tool(call.name, call.arguments),
                    })
                continue
            parsed = parse_judgment(response.content)
            if parsed is not None:
                return parsed
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": (
                    "请只输出 JSON："
                    '{"verdict":"done"|"continue"|"pause","summary":"..."}'
                ),
            })
        return JudgmentVerdict.PAUSE, "Judge 未能给出有效判定，交给人决定。"

    async def _run_tool(self, name: str, arguments: str) -> str:
        if name not in JUDGE_TOOLS or name in MUTATING_TOOLS:
            return json.dumps(
                {"ok": False, "error": f"judge cannot use {name}"},
                ensure_ascii=False,
            )
        try:
            payload = json.loads(arguments)
        except json.JSONDecodeError:
            return json.dumps(
                {"ok": False, "error": "tool arguments must be JSON"},
                ensure_ascii=False,
            )
        if type(payload) is not dict:
            return json.dumps(
                {"ok": False, "error": "tool arguments must be an object"},
                ensure_ascii=False,
            )
        result = await self._runner.execute(name, payload)
        return json.dumps(result, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class HostJudgeRunner:
    executor: ToolRunner
    gateway: ArtifactGateway
    session_id: str

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> object:
        if name == "read_artifact":
            return _read_artifact(
                self.gateway.for_session(self.session_id),
                arguments,
            )
        return await self.executor.execute(name, arguments)


def make_isolated_judge(
    llm: LLMApi,
    model: str,
    tool_schemas: Sequence[dict[str, object]],
    executor: ToolRunner,
    gateway: ArtifactGateway,
) -> JudgeFn:
    async def judge(
        events: tuple,
        criteria: CriteriaCommitted,
        step_id: str,
    ) -> tuple[JudgmentVerdict, str]:
        inner = IsolatedJudge(
            llm,
            model,
            tool_schemas,
            HostJudgeRunner(executor, gateway, events[0].session_id),
        )
        return await inner(events, criteria, step_id)

    return judge


def _read_artifact(store, arguments: Mapping[str, object]) -> object:
    artifact_id = arguments.get("artifact_id")
    if not is_valid_artifact_id(artifact_id):
        return {
            "ok": False,
            "code": "INVALID_ARGUMENT",
            "error": "artifact_id 格式无效",
        }
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 3000)
    if type(offset) is not int or offset < 0:
        return {
            "ok": False,
            "code": "INVALID_ARGUMENT",
            "error": "offset 必须是 >= 0 的整数",
        }
    if type(limit) is not int or not 1 <= limit <= 3000:
        return {
            "ok": False,
            "code": "INVALID_ARGUMENT",
            "error": "limit 必须是 1 到 3000 的整数",
        }
    try:
        chunk = store.read(artifact_id, offset, limit)
    except ArtifactNotFoundError:
        return {
            "ok": False,
            "code": "ARTIFACT_NOT_FOUND",
            "error": f"runtime artifact 不存在: {artifact_id}",
        }
    except ArtifactOffsetOutOfRangeError as exc:
        return {
            "ok": False,
            "code": "ARTIFACT_OFFSET_OUT_OF_RANGE",
            "error": str(exc),
        }
    return {
        "ok": True,
        "code": "ARTIFACT_READ",
        "data": {
            "artifact_id": chunk.artifact_id,
            "content": chunk.content,
            "offset": chunk.offset,
            "next_offset": chunk.next_offset,
            "total_chars": chunk.total_chars,
            "truncated": chunk.truncated,
        },
    }


@dataclass(frozen=True, slots=True)
class JudgmentPolicy:
    judge: JudgeFn
    notify: Callable[[str], Awaitable[None] | None] | None = None

    async def on_user_message(
        self,
        runtime: AgentRuntime,
        session_id: str,
        text: str,
    ) -> None:
        events = await runtime.snapshot(session_id)
        current = current_criteria(events)
        intent = classify_user_intent(current, text)
        proposed = criteria_after_intent(
            current,
            intent,
            text,
            collect_facts(events),
        )
        if proposed is None:
            return
        await runtime.record_fact(session_id, criteria_fact(proposed))

    async def sync(self, runtime: AgentRuntime, session_id: str) -> None:
        events = await runtime.snapshot(session_id)
        proposed = next_criteria_from_facts(events)
        if proposed is None:
            return
        await runtime.record_fact(session_id, criteria_fact(proposed))

    async def gate(
        self,
        runtime: AgentRuntime,
        session_id: str,
    ) -> CompletionGate:
        events = await runtime.snapshot(session_id)
        criteria = current_criteria(events)
        step_id = latest_complete_step_id(events)
        if (
            criteria is None
            or not criteria.strict_completion
            or step_id is None
        ):
            return CompletionGate.FINALIZE
        state = await runtime.state(session_id)
        if not state.steps or state.steps[-1].step_id != step_id:
            return CompletionGate.FINALIZE
        if state.steps[-1].decision.lifecycle_intent is not LifecycleIntent.COMPLETE:
            return CompletionGate.FINALIZE
        existing = judgment_for_step(events, step_id)
        if existing is None:
            verdict, summary = await self.judge(events, criteria, step_id)
            existing = JudgmentCommitted(
                criteria_version=criteria.version,
                step_id=step_id,
                verdict=verdict,
                summary=summary,
            )
            await runtime.record_fact(session_id, judgment_fact(existing))
            await self._notify(existing)
        if existing.verdict is JudgmentVerdict.PAUSE:
            return CompletionGate.PAUSE
        return CompletionGate.FINALIZE

    async def _notify(
        self,
        judgment: JudgmentCommitted,
    ) -> None:
        if self.notify is None:
            return
        notified = self.notify(
            f"[judge {judgment.verdict.value}] {judgment.summary}"
        )
        if isinstance(notified, Awaitable):
            await notified


def judge_tool_schemas(
    schemas: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        schema
        for schema in schemas
        if _schema_name(schema) in JUDGE_TOOLS
    ]


def _schema_name(schema: Mapping[str, object]) -> str:
    if set(schema) != {"type", "function"} or schema["type"] != "function":
        raise ValueError("tool schema envelope 无效")
    function = schema["function"]
    if not isinstance(function, Mapping):
        raise ValueError("tool schema function 必须是 object")
    name = function.get("name")
    if type(name) is not str or not name:
        raise ValueError("tool schema name 必须是非空 string")
    return name


def _judge_user_prompt(events: tuple, criteria: CriteriaCommitted, step_id: str) -> str:
    inferred = "\n".join(
        f"- [{item.status.value}] {item.criterion_id}: {item.text}"
        for item in criteria.inferred
    ) or "无"
    return (
        f"判定 step `{step_id}` 是否达到当前冻结标准。\n"
        f"user 目标：{criteria.user_objective}\n"
        f"criteria_version：{criteria.version}\n"
        f"strict_completion：true\n"
        f"inferred：\n{inferred}\n"
        f"本 Session 已记录的工具结果：\n{format_facts_for_judge(events)}"
    )


def _assistant_tool_message(response) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": response.content or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.arguments,
                },
            }
            for call in response.calls
        ],
    }
