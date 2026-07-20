from __future__ import annotations

import json
from core.model_call.types import InvalidLLMResponse, LLMResponse
from core.planning.plan import Plan, PlanStep

MIN_PLAN_STEPS = 2
MAX_PLAN_STEPS = 6


class InvalidPlanResponse(ValueError):
    pass


PLANNER_SYSTEM_PROMPT = """
你是任务规划器。

你的唯一职责是根据用户目标制定执行计划。

你不执行计划，不调用工具，不回答用户的问题，
也不能声称已经完成任何步骤。

你的输出必须是一个 JSON 对象，不要输出 Markdown、
代码块、解释文字、思考过程或 JSON 之外的任何内容。

输出结构：
{"goal":"用户目标","steps":["步骤1","步骤2"]}

约束：
- goal 必须是非空字符串。
- steps 必须包含 2 到 6 个简短的意图阶段。
- steps 不得包含执行结果、status、id 或 note。
- 第一个非空字符必须是 {，最后一个非空字符必须是 }。
""".strip()


def create_plan(response: LLMResponse) -> Plan:
    if response.type != "text":
        raise InvalidLLMResponse(
            "invalid_plan_response",
            "planner response type must be text",
        )
    try:
        plan = parse_plan_response(response.content)
    except InvalidPlanResponse as exc:
        raw_preview = response.content[:2000]
        raise InvalidLLMResponse(
            "invalid_plan_response",
            f"{exc}; raw_response={raw_preview!r}",
        ) from exc
    return plan


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
