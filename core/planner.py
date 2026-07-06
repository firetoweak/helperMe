from __future__ import annotations

from core.plan import Plan, PlanStep


def create_plan(user_message: str, conversation):
    # 后续LLM 动态拆解任务
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


if __name__ == "__main__":
    plan = create_plan("帮我优化工具描述", None)
    print(format_plan_for_model(plan))