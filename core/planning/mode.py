from __future__ import annotations

from core.messages import Conversation
from core.tools_runtime.state import ToolStep, ToolsState
from core.planning.planner import build_runtime_messages, create_plan, format_plan_for_model

WRITE_TOOL_NAMES = {"apply_patch", "replace_all", "write_file"}
VERIFY_TOOL_NAMES = {"get_changes"}


class PlanningMode:
    def __init__(self) -> None:
        self.plan = None
        self.reflection_requested = False
        self.tool_phase_started = False
        self.write_phase_started = False
        self.verify_phase_started = False

    def start(self, user_message: str, conversation: Conversation, llm_client, model: str) -> None:
        self.reflection_requested = False
        self.tool_phase_started = False
        self.write_phase_started = False
        self.verify_phase_started = False
        self.plan = create_plan(user_message, conversation, llm_client, model)
        self.plan.mark_doing(1, "开始执行任务")

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        if self.plan is None:
            return messages
        return build_runtime_messages(messages, format_plan_for_model(self.plan))

    def on_assistant_text(self, conversation: Conversation) -> bool:
        if self.plan is None or self.reflection_requested:
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
        if self.plan is None:
            return None

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
        if self.plan is None:
            return None
        return {"plan": self.plan.to_dict()}
