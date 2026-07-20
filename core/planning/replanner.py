from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from core.model_call.types import InvalidLLMResponse, LLMResponse
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


REPLANNER_INSTRUCTION = (
    "你是 replanner，只负责判断当前执行计划是否需要修改。"
    "如果失败不影响原计划，返回 "
    '{"action":"keep","reason":"原因"}。'
    "如果必须修改计划，返回 "
    '{"action":"revise","reason":"原因","steps":["后续步骤1"]}。'
    "只返回 JSON，不要输出 Markdown，不要解释。"
    "第一个非空字符必须是 {，最后一个非空字符必须是 }，"
    "禁止输出思考过程、代码围栏或任何 JSON 之外的前后缀文本。"
    "revise 的 steps 必须包含 1 到 6 个简短的后续意图阶段，"
    "不要重复已经完成的步骤，不要包含 status/id/note，不要修改目标。"
)


def build_replanner_instruction(
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


def replan(response: LLMResponse) -> ReplanDecision:
    if response.type != "text":
        raise InvalidLLMResponse(
            "invalid_replan_response",
            "replanner response type must be text",
        )

    try:
        decision = parse_replan_response(response.content)
    except InvalidReplanResponse as exc:
        raw_preview = response.content[:2000]
        raise InvalidLLMResponse(
            "invalid_replan_response",
            f"{exc}; raw_response={raw_preview!r}",
        ) from exc

    return decision


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
