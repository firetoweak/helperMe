from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from core.context import (
    ContextComposition,
    ContextPreparationService,
    ContextState,
    MicroCompactionTrace,
    SummaryCompaction,
)
from core.messages import Conversation
from core.model_call.service import (
    ModelCallBlocked,
    ModelCallRequest,
    ModelCallService,
)
from core.model_call.types import LLMUsage
from core.planning.plan import Plan
from core.planning.planner import format_plan_for_model
from core.tools_runtime.tools_state import ToolStep

ReplanAction = Literal["keep", "revise"]

MIN_REPLAN_STEPS = 1
MAX_REPLAN_STEPS = 6


class InvalidReplanResponse(ValueError):
    pass


@dataclass(frozen=True)
class ReplanDecision:
    action: ReplanAction
    reason: str
    steps: list[str]


@dataclass(frozen=True)
class ReplanCallResult:
    decision: ReplanDecision
    usage: LLMUsage
    context_state: ContextState
    summary_compaction: SummaryCompaction | None
    composition: ContextComposition
    micro_compaction_trace: MicroCompactionTrace


@dataclass(frozen=True)
class ReplanCallBlocked:
    blocked: ModelCallBlocked
    context_state: ContextState
    summary_compaction: SummaryCompaction | None
    composition: ContextComposition
    micro_compaction_trace: MicroCompactionTrace


REPLANNER_INSTRUCTION = (
    "你是 replanner，只负责判断当前执行计划是否需要修改。"
    "如果失败不影响原计划，返回 "
    '{"action":"keep","reason":"原因"}。'
    "如果必须修改计划，返回 "
    '{"action":"revise","reason":"原因","steps":["后续步骤1"]}。'
    "只返回 JSON，不要输出 Markdown，不要解释。"
    "revise 的 steps 必须包含 1 到 6 个简短的后续意图阶段，"
    "不要重复已经完成的步骤，不要包含 status/id/note，不要修改目标。"
)


def _build_replanner_instruction(
    plan: Plan,
    failed_steps: list[ToolStep],
) -> str:
    lines = [
        REPLANNER_INSTRUCTION,
        "",
        f"当前计划版本：revision={plan.revision}",
        format_plan_for_model(plan),
        "",
        "本轮失败工具：",
    ]
    for step in failed_steps:
        lines.append(
            f"- tool={step.name}, code={step.code}, error={step.error}"
        )
    return "\n".join(lines)


def replan(
    conversation: Conversation,
    plan: Plan,
    failed_steps: list[ToolStep],
    context_preparation: ContextPreparationService,
    context_state: ContextState,
    model_calls: ModelCallService,
    model: str,
    level2_boundary_message_id: str | None = None,
) -> ReplanCallResult | ReplanCallBlocked:
    prepared = context_preparation.prepare(
        conversation_records=conversation.records,
        context_state=context_state,
        runtime_instructions=[
            _build_replanner_instruction(plan, failed_steps)
        ],
        tools=[],
        level2_boundary_message_id=level2_boundary_message_id,
    )
    if prepared.blocked_assessment is not None:
        return ReplanCallBlocked(
            blocked=ModelCallBlocked(prepared.blocked_assessment),
            context_state=prepared.context_state,
            summary_compaction=prepared.summary_compaction,
            composition=prepared.composition,
            micro_compaction_trace=prepared.micro_compaction_trace,
        )

    outcome = model_calls.call(
        ModelCallRequest(
            context=prepared.model_context,
            tools=[],
        ),
        model,
    )
    if isinstance(outcome, ModelCallBlocked):
        return ReplanCallBlocked(
            blocked=outcome,
            context_state=prepared.context_state,
            summary_compaction=prepared.summary_compaction,
            composition=prepared.composition,
            micro_compaction_trace=prepared.micro_compaction_trace,
        )

    response = outcome.response
    if response.type != "text":
        raise InvalidReplanResponse("replanner response type must be text")

    return ReplanCallResult(
        decision=parse_replan_response(response.content),
        usage=outcome.usage,
        context_state=prepared.context_state,
        summary_compaction=prepared.summary_compaction,
        composition=prepared.composition,
        micro_compaction_trace=prepared.micro_compaction_trace,
    )


def parse_replan_response(content: str) -> ReplanDecision:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidReplanResponse(
            f"replan response returned invalid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise InvalidReplanResponse("replan response must be a JSON object")

    action = payload.get("action")
    if action not in ("keep", "revise"):
        raise InvalidReplanResponse(
            "replan action must be 'keep' or 'revise'"
        )

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidReplanResponse(
            "replan reason must be a non-empty string"
        )
    reason = reason.strip()

    raw_steps = payload.get("steps")

    if action == "keep":
        if raw_steps is None or raw_steps == []:
            return ReplanDecision(action="keep", reason=reason, steps=[])
        raise InvalidReplanResponse(
            "replan keep must not include non-empty steps"
        )

    if not isinstance(raw_steps, list):
        raise InvalidReplanResponse("replan steps must be a list")

    steps: list[str] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, str) or not item.strip():
            raise InvalidReplanResponse(
                f"replan step[{index}] must be a non-empty string"
            )
        steps.append(item.strip())

    if len(steps) < MIN_REPLAN_STEPS:
        raise InvalidReplanResponse(
            f"replan must return at least {MIN_REPLAN_STEPS} steps"
        )
    if len(steps) > MAX_REPLAN_STEPS:
        raise InvalidReplanResponse(
            f"replan must return at most {MAX_REPLAN_STEPS} steps"
        )

    return ReplanDecision(action="revise", reason=reason, steps=steps)
