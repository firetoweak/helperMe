from __future__ import annotations

from core.context import ContextPreparationService, ContextState
from core.messages import Conversation
from core.model_call.service import ModelCallService
from core.runtime_modes.base import RuntimeModeStartResult
from core.tools_runtime.tools_state import ToolStep, ToolsState
from core.planning.plan import Plan
from core.planning.planner import PlanCallBlocked, create_plan, format_plan_for_model

WRITE_TOOL_NAMES = {"apply_patch", "replace_all", "write_file"}
VERIFY_TOOL_NAMES = {"get_changes"}


class PlanningMode:
    def __init__(self) -> None:
        self.plan: Plan
        self.reflection_requested = False
        self.tool_phase_started = False
        self.write_phase_started = False
        self.verify_phase_started = False

    def start(
        self,
        conversation: Conversation,
        model_calls: ModelCallService,
        model: str,
        context_preparation: ContextPreparationService,
        context_state: ContextState,
        level2_boundary_message_id: str | None,
    ) -> RuntimeModeStartResult:
        self.reflection_requested = False
        self.tool_phase_started = False
        self.write_phase_started = False
        self.verify_phase_started = False
        outcome = create_plan(
            conversation=conversation,
            context_preparation=context_preparation,
            context_state=context_state,
            level2_boundary_message_id=level2_boundary_message_id,
            model_calls=model_calls,
            model=model,
        )
        if isinstance(outcome, PlanCallBlocked):
            return RuntimeModeStartResult(
                context_state=outcome.context_state,
                blocked=outcome.blocked,
                summary_compaction=outcome.summary_compaction,
                composition=outcome.composition,
                micro_compaction_trace=outcome.micro_compaction_trace,
            )
        self.plan = outcome.plan
        self.plan.mark_doing(1, "开始执行任务")
        return RuntimeModeStartResult(
            context_state=outcome.context_state,
            usage=outcome.usage,
            summary_compaction=outcome.summary_compaction,
            composition=outcome.composition,
            micro_compaction_trace=outcome.micro_compaction_trace,
        )

    def runtime_instructions(self) -> list[str]:
        return [format_plan_for_model(self.plan)]

    def on_assistant_text(self, conversation: Conversation) -> bool:
        if self.reflection_requested:
            return False

        self.reflection_requested = True
        self.plan.advance_to_next(
            done_note="模型已形成初步回答",
            doing_note="正在最终复核计划完成情况",
        )
        conversation.add_user(
            "请在最终回答前根据当前执行计划做一次简短检查："
            "哪些步骤已完成？是否还有未完成步骤？"
            "如果有未完成步骤，最终回答必须明确说明。"
            "检查后再给出最终回答。"
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

        if any(step.ok is False for step in batch_steps):
            conversation.add_user(
                "刚才有工具调用失败。请根据工具返回的 code/error/hint 调整下一步。"
                "如果原计划不再适用，请先说明调整后的计划，再继续执行。"
            )

    def checkpoint_data(self) -> dict | None:
        return {"plan": self.plan.to_dict()}
