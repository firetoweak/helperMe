"""当前任务的判定标准：分类来自 Journal 事实，inferred 只追加版本。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from helperme.runtime.events import (
    CommandOutcomeReceived,
    DomainFactCommitted,
    Event,
    StepCommitted,
    UserMessageReceived,
)
from helperme.runtime.model import (
    CancelTool,
    InvokeTool,
    LifecycleIntent,
    OutcomeStatus,
)


MUTATING_TOOLS = frozenset({"write_file", "apply_patch", "replace_all"})
COMMAND_TOOLS = frozenset({"execute_command"})
CRITERIA_FACT_TYPE = "helperme.criteria.committed.v1"
JUDGMENT_FACT_TYPE = "helperme.judgment.committed.v1"


class CriterionStatus(str, Enum):
    ACTIVE = "active"
    DEFERRED = "deferred"


class CriteriaSource(str, Enum):
    USER = "user"
    CLASSIFIER = "classifier"
    USER_REVISION = "user_revision"


class JudgmentVerdict(str, Enum):
    DONE = "done"
    CONTINUE = "continue"
    PAUSE = "pause"


def _require_text(value: object, name: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class InferredCriterion:
    criterion_id: str
    text: str
    status: CriterionStatus = CriterionStatus.ACTIVE

    def __post_init__(self) -> None:
        _require_text(self.criterion_id, "criterion_id")
        _require_text(self.text, "criterion text")
        if type(self.status) is not CriterionStatus:
            raise TypeError("criterion status must be CriterionStatus")


@dataclass(frozen=True, slots=True)
class CriteriaCommitted:
    version: int
    user_objective: str
    strict_completion: bool
    inferred: tuple[InferredCriterion, ...]
    source: CriteriaSource

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version < 1:
            raise ValueError("criteria version must be positive")
        _require_text(self.user_objective, "user objective")
        if type(self.strict_completion) is not bool:
            raise TypeError("strict_completion must be bool")
        if type(self.inferred) is not tuple or any(
            type(item) is not InferredCriterion for item in self.inferred
        ):
            raise TypeError("inferred must contain InferredCriterion")
        ids = tuple(item.criterion_id for item in self.inferred)
        if len(ids) != len(set(ids)):
            raise ValueError("inferred criterion ids contain duplicates")
        if type(self.source) is not CriteriaSource:
            raise TypeError("criteria source must be CriteriaSource")


@dataclass(frozen=True, slots=True)
class JudgmentCommitted:
    criteria_version: int
    step_id: str
    verdict: JudgmentVerdict
    summary: str

    def __post_init__(self) -> None:
        if type(self.criteria_version) is not int or self.criteria_version < 1:
            raise ValueError("criteria version must be positive")
        _require_text(self.step_id, "step_id")
        if type(self.verdict) is not JudgmentVerdict:
            raise TypeError("verdict must be JudgmentVerdict")
        _require_text(self.summary, "judgment summary")


def criteria_fact(snapshot: CriteriaCommitted) -> DomainFactCommitted:
    return DomainFactCommitted(
        fact_type=CRITERIA_FACT_TYPE,
        data={
            "version": snapshot.version,
            "user_objective": snapshot.user_objective,
            "strict_completion": snapshot.strict_completion,
            "inferred": [
                {
                    "criterion_id": item.criterion_id,
                    "text": item.text,
                    "status": item.status.value,
                }
                for item in snapshot.inferred
            ],
            "source": snapshot.source.value,
        },
    )


def judgment_fact(judgment: JudgmentCommitted) -> DomainFactCommitted:
    return DomainFactCommitted(
        fact_type=JUDGMENT_FACT_TYPE,
        data={
            "criteria_version": judgment.criteria_version,
            "step_id": judgment.step_id,
            "verdict": judgment.verdict.value,
            "summary": judgment.summary,
        },
        requests_decision=judgment.verdict is JudgmentVerdict.CONTINUE,
    )


def criteria_from_fact(payload: object) -> CriteriaCommitted | None:
    if not isinstance(payload, DomainFactCommitted) or (
        payload.fact_type != CRITERIA_FACT_TYPE
    ):
        return None
    if not isinstance(payload.data, Mapping):
        raise ValueError("invalid criteria fact data")
    data = payload.data
    if set(data) != {
        "version",
        "user_objective",
        "strict_completion",
        "inferred",
        "source",
    }:
        raise ValueError("invalid criteria fact fields")
    inferred_raw = data["inferred"]
    if (
        not isinstance(inferred_raw, tuple)
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"criterion_id", "text", "status"}
            for item in inferred_raw
        )
    ):
        raise ValueError("invalid inferred criteria facts")
    try:
        inferred = tuple(
            InferredCriterion(
                item["criterion_id"],
                item["text"],
                CriterionStatus(item["status"]),
            )
            for item in inferred_raw
        )
        return CriteriaCommitted(
            version=data["version"],
            user_objective=data["user_objective"],
            strict_completion=data["strict_completion"],
            inferred=inferred,
            source=CriteriaSource(data["source"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid criteria fact") from exc


def judgment_from_fact(payload: object) -> JudgmentCommitted | None:
    if not isinstance(payload, DomainFactCommitted) or (
        payload.fact_type != JUDGMENT_FACT_TYPE
    ):
        return None
    if not isinstance(payload.data, Mapping) or set(payload.data) != {
        "criteria_version",
        "step_id",
        "verdict",
        "summary",
    }:
        raise ValueError("invalid judgment fact fields")
    data = payload.data
    try:
        return JudgmentCommitted(
            criteria_version=data["criteria_version"],
            step_id=data["step_id"],
            verdict=JudgmentVerdict(data["verdict"]),
            summary=data["summary"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid judgment fact") from exc

CRITERION_WORKSPACE = InferredCriterion(
    "inf-workspace",
    "工作区改动必须用 get_changes 或再次读取确认，不能只凭自述。",
)
CRITERION_VERIFY = InferredCriterion(
    "inf-verify",
    "若改动了程序行为，必须用测试或等价命令验证；没有证据不算完成。",
)

_RELAX_MARKERS = (
    "一会再说",
    "一会儿再说",
    "先不测",
    "先改完",
    "测试先放",
    "先跳过测试",
    "测试一会",
    "测试一会儿",
)
_CHANGE_MARKERS = (
    "换个任务",
    "改做",
    "不要这个了",
    "别做这个了",
    "新任务",
    "另外一件事",
)


@dataclass(frozen=True, slots=True)
class StreamFacts:
    wrote_files: bool
    ran_commands: bool
    last_user_message: str | None


@dataclass(frozen=True, slots=True)
class UserIntent:
    kind: str
    deferred_ids: tuple[str, ...] = ()


def command_names(events: tuple[Event, ...]) -> dict[str, str]:
    names: dict[str, str] = {}
    for event in events:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            continue
        for command in payload.step.commands:
            effect = command.effect
            if isinstance(effect, InvokeTool):
                names[command.command_id] = effect.name
            elif isinstance(effect, CancelTool):
                names[command.command_id] = "cancel_tool"
            else:
                raise TypeError(type(effect).__name__)
    return names


def collect_facts(events: tuple[Event, ...]) -> StreamFacts:
    names = command_names(events)
    wrote = False
    ran = False
    last_user: str | None = None
    for event in events:
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            last_user = payload.content
            continue
        if not isinstance(payload, CommandOutcomeReceived):
            continue
        if payload.outcome.status is not OutcomeStatus.SUCCEEDED:
            continue
        name = names[payload.command_id]
        if name in MUTATING_TOOLS:
            wrote = True
        if name in COMMAND_TOOLS:
            ran = True
    return StreamFacts(wrote, ran, last_user)


def current_criteria(events: tuple[Event, ...]) -> CriteriaCommitted | None:
    latest: CriteriaCommitted | None = None
    for event in events:
        snapshot = criteria_from_fact(event.payload)
        if snapshot is not None:
            latest = snapshot
    return latest


def judgment_for_step(
    events: tuple[Event, ...],
    step_id: str,
) -> JudgmentCommitted | None:
    latest: JudgmentCommitted | None = None
    for event in events:
        judgment = judgment_from_fact(event.payload)
        if judgment is not None and judgment.step_id == step_id:
            latest = judgment
    return latest


def latest_complete_step_id(events: tuple[Event, ...]) -> str | None:
    latest: str | None = None
    for event in events:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            continue
        if payload.step.decision.lifecycle_intent is LifecycleIntent.COMPLETE:
            latest = payload.step.step_id
    return latest


def classify_user_intent(
    current: CriteriaCommitted | None,
    text: str,
) -> UserIntent:
    if current is None:
        return UserIntent("seed")
    if any(marker in text for marker in _CHANGE_MARKERS):
        return UserIntent("change_objective")
    if any(marker in text for marker in _RELAX_MARKERS):
        if "测试" in text:
            return UserIntent("relax_inferred", ("inf-verify",))
        active = tuple(
            item.criterion_id
            for item in current.inferred
            if item.status is CriterionStatus.ACTIVE
        )
        return UserIntent("relax_inferred", active)
    return UserIntent("proceed")


def inferred_from_facts(facts: StreamFacts) -> tuple[InferredCriterion, ...]:
    items: list[InferredCriterion] = []
    if facts.wrote_files:
        items.append(CRITERION_WORKSPACE)
    if facts.wrote_files or facts.ran_commands:
        items.append(CRITERION_VERIFY)
    return tuple(items)


def _merge_inferred(
    previous: tuple[InferredCriterion, ...],
    compiled: tuple[InferredCriterion, ...],
) -> tuple[InferredCriterion, ...]:
    known = {item.criterion_id: item for item in previous}
    merged: list[InferredCriterion] = []
    seen: set[str] = set()
    for item in compiled:
        existing = known.get(item.criterion_id)
        if existing is not None:
            merged.append(existing)
        else:
            merged.append(item)
        seen.add(item.criterion_id)
    for item in previous:
        if item.criterion_id not in seen:
            merged.append(item)
    return tuple(merged)


def _same_snapshot(
    current: CriteriaCommitted,
    next_snapshot: CriteriaCommitted,
) -> bool:
    return (
        current.user_objective == next_snapshot.user_objective
        and current.strict_completion == next_snapshot.strict_completion
        and current.inferred == next_snapshot.inferred
    )


def next_criteria_from_facts(
    events: tuple[Event, ...],
) -> CriteriaCommitted | None:
    facts = collect_facts(events)
    current = current_criteria(events)
    compiled = inferred_from_facts(facts)
    strict = facts.wrote_files or facts.ran_commands
    if current is None:
        if facts.last_user_message is None:
            return None
        return CriteriaCommitted(
            version=1,
            user_objective=facts.last_user_message,
            strict_completion=strict,
            inferred=compiled,
            source=CriteriaSource.USER,
        )
    merged = _merge_inferred(current.inferred, compiled)
    proposed = CriteriaCommitted(
        version=current.version + 1,
        user_objective=current.user_objective,
        strict_completion=current.strict_completion or strict,
        inferred=merged,
        source=CriteriaSource.CLASSIFIER,
    )
    if _same_snapshot(current, proposed):
        return None
    return proposed


def criteria_after_intent(
    current: CriteriaCommitted | None,
    intent: UserIntent,
    text: str,
    facts: StreamFacts,
) -> CriteriaCommitted | None:
    if intent.kind == "seed" or current is None:
        return CriteriaCommitted(
            version=1 if current is None else current.version + 1,
            user_objective=text,
            strict_completion=facts.wrote_files or facts.ran_commands,
            inferred=inferred_from_facts(facts),
            source=CriteriaSource.USER,
        )
    if intent.kind == "change_objective":
        return CriteriaCommitted(
            version=current.version + 1,
            user_objective=text,
            strict_completion=facts.wrote_files or facts.ran_commands,
            inferred=inferred_from_facts(facts),
            source=CriteriaSource.USER_REVISION,
        )
    if intent.kind == "relax_inferred":
        deferred = set(intent.deferred_ids)
        inferred = tuple(
            InferredCriterion(
                item.criterion_id,
                item.text,
                CriterionStatus.DEFERRED
                if item.criterion_id in deferred
                else item.status,
            )
            for item in current.inferred
        )
        proposed = CriteriaCommitted(
            version=current.version + 1,
            user_objective=current.user_objective,
            strict_completion=current.strict_completion,
            inferred=inferred,
            source=CriteriaSource.USER_REVISION,
        )
        if _same_snapshot(current, proposed):
            return None
        return proposed
    return None


def format_criteria_for_worker(snapshot: CriteriaCommitted | None) -> str:
    if snapshot is None:
        return ""
    lines = [
        "当前判定标准是 Journal 事实，只读。你不能修改、否决或忽略分类结果。",
        f"user 目标：{snapshot.user_objective}",
        f"strict_completion：{'true' if snapshot.strict_completion else 'false'}",
        f"criteria_version：{snapshot.version}",
    ]
    if snapshot.inferred:
        lines.append("inferred：")
        for item in snapshot.inferred:
            lines.append(
                f"- [{item.status.value}] {item.criterion_id}: {item.text}"
            )
        lines.append(
            "deferred 表示人已推迟该条，不要自行恢复。"
        )
    else:
        lines.append("inferred：无")
    if snapshot.strict_completion:
        lines.append(
            "本次需要严格收口。完成前必须留下可核对证据；"
            "最终是否完成由独立 Judge 判定，自述不算证据。"
        )
    else:
        lines.append("本次默认收口由人决定；没有新的用户话时不要自行宣称任务结束。")
    return "\n".join(lines)


def format_facts_for_judge(events: tuple[Event, ...]) -> str:
    names = command_names(events)
    lines: list[str] = []
    for event in events:
        payload = event.payload
        if not isinstance(payload, CommandOutcomeReceived):
            continue
        name = names[payload.command_id]
        status = payload.outcome.status.value
        lines.append(f"- {name} {payload.command_id}: {status}")
    return "\n".join(lines) if lines else "（本 Stream 尚无工具结果）"
