from __future__ import annotations

from core.messages import Conversation
from core.model_call.types import LLMResponse
from core.tools_runtime.tools_state import ToolStep, ToolsState
from core.planning.plan import Plan
from core.planning.planner import (
    PLANNER_SYSTEM_PROMPT,
    create_plan,
    format_plan_for_model,
)
from core.planning.replanner import (
    build_replanner_instruction,
    replan,
)

WRITE_TOOL_NAMES = {"apply_patch", "replace_all", "write_file"}
VERIFY_TOOL_NAMES = {"get_changes"}


class PlanningMode:
    def __init__(self) -> None:
        self.plan: Plan
        self.reflection_requested = False
        self.tool_phase_started = False
        self.write_phase_started = False
        self.verify_phase_started = False

    def start(self, conversation: Conversation) -> str | None:
        self.reflection_requested = False
        self.tool_phase_started = False
        self.write_phase_started = False
        self.verify_phase_started = False
        return PLANNER_SYSTEM_PROMPT

    def accept_start_response(self, response: LLMResponse) -> dict | None:
        self.plan = create_plan(response)
        self.plan.mark_doing(1, "开始执行任务")
        return {"plan": self.plan.to_dict()}

    def runtime_instructions(self) -> list[str]:
        return [format_plan_for_model(self.plan)]

    def on_assistant_text(self, conversation: Conversation) -> bool:
        if self.reflection_requested:
            self.plan.complete_remaining("已完成最终回答")
            return False

        self.reflection_requested = True
        self.plan.advance_to_next(
            done_note="模型已形成初步回答",
            doing_note="正在最终复核计划完成情况",
        )
        conversation.add_user(
            "请在内部根据当前执行计划检查候选回答是否完整，"
            "不要输出检查过程。"
            "检查后输出一份完整、可独立阅读的最终答案，"
            "其中必须包含用户实际需要的内容。"
            "禁止引用上一条回复，禁止使用‘如前所述’等省略表达。"
            "如果仍有未完成事项，应在最终答案中直接说明。"
            "仅输出最终答案。"
        )
        return True

    def after_tool_batch(
        self,
        conversation: Conversation,
        tools_state: ToolsState,
        batch_steps: list[ToolStep],
    ) -> None:
        if batch_steps and not self.tool_phase_started:
            self.tool_phase_started = True
            self.plan.advance_to_next(
                done_note="模型已开始基于计划选择工具",
                doing_note="正在通过工具收集信息或执行操作",
            )

        if (
            not self.write_phase_started
            and any(step.name in WRITE_TOOL_NAMES and step.ok is True for step in batch_steps)
        ):
            self.write_phase_started = True
            self.plan.advance_to_next(
                done_note="已收集到执行所需信息",
                doing_note="正在执行必要操作",
            )

        if (
            not self.verify_phase_started
            and any(step.name in VERIFY_TOOL_NAMES and step.ok is True for step in batch_steps)
        ):
            self.verify_phase_started = True
            self.plan.advance_to_next(
                done_note="已完成主要执行操作",
                doing_note="正在验证结果并准备总结",
            )

    def handle_tool_failures(
        self,
        conversation: Conversation,
        failed_steps: list[ToolStep],
    ) -> str | None:
        return build_replanner_instruction(self.plan, failed_steps)

    def accept_tool_failure_response(
        self,
        response: LLMResponse,
    ) -> dict | None:
        before_plan = self.plan.to_dict()
        decision = replan(response)
        if decision.action == "revise":
            self.plan.revise(decision.reason, decision.steps)

        return {
            "trigger": "tool_failure",
            "action": decision.action,
            "reason": decision.reason,
            "before_plan": before_plan,
            "after_plan": self.plan.to_dict(),
        }

    def checkpoint_data(self) -> dict | None:
        return {"plan": self.plan.to_dict()}
