from __future__ import annotations

import json
from dataclasses import dataclass

from core.context import ContextPreparationService, ContextState, SummaryCompaction
from core.messages import Conversation
from core.model_call.service import (
    ModelCallBlocked,
    ModelCallRequest,
    ModelCallService,
)
from core.model_call.types import LLMUsage
from core.planning.plan import Plan, PlanStep

MIN_PLAN_STEPS = 2
MAX_PLAN_STEPS = 6


class InvalidPlanResponse(ValueError):
    pass


@dataclass(frozen=True)
class PlanCallResult:
    plan: Plan
    usage: LLMUsage
    context_state: ContextState
    summary_compaction: SummaryCompaction | None


@dataclass(frozen=True)
class PlanCallBlocked:
    blocked: ModelCallBlocked
    context_state: ContextState
    summary_compaction: SummaryCompaction | None


PLANNER_INSTRUCTION = (
    "你是 planner，只负责为 agent 生成执行计划。"
    "只返回 JSON，不要输出 Markdown，不要解释。"
    "JSON 格式必须是："
    '{"goal": "目标", "steps": ["步骤1", "步骤2"]}。'
    "steps 应该是 2 到 6 个简短的意图阶段，"
    "不要包含执行结果，不要包含 status/id/note。"
)


def create_plan(
    conversation: Conversation,
    context_preparation: ContextPreparationService,
    context_state: ContextState,
    model_calls: ModelCallService,
    model: str,
    level2_boundary_message_id: str | None = None,
) -> PlanCallResult | PlanCallBlocked:
    prepared = context_preparation.prepare(
        conversation_records=conversation.records,
        context_state=context_state,
        runtime_instructions=[PLANNER_INSTRUCTION],
        tools=[],
        level2_boundary_message_id=level2_boundary_message_id,
    )
    if prepared.blocked_assessment is not None:
        return PlanCallBlocked(
            blocked=ModelCallBlocked(prepared.blocked_assessment),
            context_state=prepared.context_state,
            summary_compaction=prepared.summary_compaction,
        )
    outcome = model_calls.call(
        ModelCallRequest(
            context=prepared.model_context,
            tools=[],
        ),
        model,
    )
    if isinstance(outcome, ModelCallBlocked):
        return PlanCallBlocked(
            blocked=outcome,
            context_state=prepared.context_state,
            summary_compaction=prepared.summary_compaction,
        )
    response = outcome.response

    if response.type != "text":
        raise InvalidPlanResponse("planner response type must be text")
    return PlanCallResult(
        plan=parse_plan_response(response.content),
        usage=outcome.usage,
        context_state=prepared.context_state,
        summary_compaction=prepared.summary_compaction,
    )


def format_plan_for_model(plan: Plan) -> str:
    lines = [
        "当前执行计划：",
        f"目标：{plan.goal}",
        "步骤：",
    ]
    for step in plan.steps:
        lines.append(f"{step.id}. [{step.status}] {step.text}")
    return "\n".join(lines)


def parse_plan_response(content: str) -> Plan:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidPlanResponse(f"planner returned invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise InvalidPlanResponse("planner response must be a JSON object")

    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise InvalidPlanResponse("planner goal must be a non-empty string")
    goal = goal.strip()

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise InvalidPlanResponse("planner steps must be a list")

    step_texts = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, str) or not item.strip():
            raise InvalidPlanResponse(
                f"planner step[{index}] must be a non-empty string"
            )
        text = item.strip()
        step_texts.append(text)

    if len(step_texts) < MIN_PLAN_STEPS:
        raise InvalidPlanResponse(
            f"planner must return at least {MIN_PLAN_STEPS} steps"
        )
    if len(step_texts) > MAX_PLAN_STEPS:
        raise InvalidPlanResponse(
            f"planner must return at most {MAX_PLAN_STEPS} steps"
        )

    return Plan(
        goal=goal,
        steps=[
            PlanStep(id=index, text=text)
            for index, text in enumerate(step_texts, start=1)
        ],
    )


if __name__ == "__main__":
    raise SystemExit("planner.py is not a standalone entry point")
