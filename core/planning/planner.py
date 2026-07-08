from __future__ import annotations

import json
from typing import Any

from core.planning.plan import Plan, PlanStep

MIN_PLAN_STEPS = 2
MAX_PLAN_STEPS = 6


def create_plan(
    user_message: str,
    conversation,
    llm_client=None,
    model: str | None = None,
) -> Plan:
    if llm_client is None or model is None:
        return fallback_plan(user_message)

    try:
        response = llm_client.chat(
            build_plan_messages(user_message, conversation),
            model,
            tools=None,
        )
    except Exception:
        return fallback_plan(user_message)

    if getattr(response, "type", None) != "text":
        return fallback_plan(user_message)
    return parse_plan_response(user_message, response.content)


def fallback_plan(user_message: str) -> Plan:
    return Plan(
        goal=user_message,
        steps=[
            PlanStep(id=1, text="理解用户目标和上下文"),
            PlanStep(id=2, text="收集完成任务所需的信息"),
            PlanStep(id=3, text="执行必要操作"),
            PlanStep(id=4, text="验证结果并总结"),
        ],
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


def build_runtime_messages(messages, plan_text):
    runtime_messages = [m.copy() for m in messages]

    plan_block = (
        "\n\n当前运行计划：\n"
        f"{plan_text}\n"
        "这是 agent 的执行辅助上下文，不是用户的新请求。"
    )

    if runtime_messages and runtime_messages[0].get("role") == "system":
        runtime_messages[0] = {
            **runtime_messages[0],
            "content": (runtime_messages[0].get("content") or "") + plan_block,
        }
    else:
        runtime_messages.insert(0, {
            "role": "system",
            "content": plan_block,
        })

    return runtime_messages


def build_plan_messages(user_message: str, conversation) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 planner，只负责为 agent 生成执行计划。"
                "只返回 JSON，不要输出 Markdown，不要解释。"
                "JSON 格式必须是："
                '{"goal": "目标", "steps": ["步骤1", "步骤2"]}。'
                "steps 应该是 2 到 6 个简短的意图阶段，"
                "不要包含执行结果，不要包含 status/id/note。"
            ),
        },
        {
            "role": "user",
            "content": user_message,
        },
    ]


def parse_plan_response(user_message: str, content: str) -> Plan:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return fallback_plan(user_message)

    if not isinstance(payload, dict):
        return fallback_plan(user_message)

    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        goal = user_message
    else:
        goal = goal.strip()

    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return fallback_plan(user_message)

    step_texts = []
    for item in raw_steps:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            step_texts.append(text)

    if len(step_texts) < MIN_PLAN_STEPS:
        return fallback_plan(user_message)

    step_texts = step_texts[:MAX_PLAN_STEPS]

    return Plan(
        goal=goal,
        steps=[
            PlanStep(id=index, text=text)
            for index, text in enumerate(step_texts, start=1)
        ],
    )


if __name__ == "__main__":
    plan = create_plan("帮我优化工具描述", None)
    print(format_plan_for_model(plan))
