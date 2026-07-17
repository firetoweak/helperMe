from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

from core.context.budget import BudgetAssessment, ContextBudget
from core.context.composition import ContextComposition, ToolResultWindowStats
from core.context.manager import ContextManager, ContextRequest, ModelContext
from core.context.micro_compaction_policy import (
    MicroCompactionDecision,
    MicroCompactionPolicy,
)
from core.context.state import ContextState
from core.messages import ConversationMessage


SUMMARY_INSTRUCTION = (
    "请将以下既有会话压缩为一段准确、简洁的工作交接摘要。"
    "必须保留用户目标与约束、已完成和待完成状态、关键工具事实及必要标识。"
    "不要添加原文中不存在的信息，只输出摘要正文。"
)


@dataclass(frozen=True)
class SummaryGeneration:
    summary: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class SummaryGenerationBlocked:
    assessment: BudgetAssessment


class ContextSummaryGenerator(Protocol):
    def generate(
        self,
        model_context: ModelContext,
    ) -> SummaryGeneration | SummaryGenerationBlocked:
        ...


@dataclass(frozen=True)
class SummaryCompaction:
    boundary_message_id: str
    before: BudgetAssessment
    after: BudgetAssessment
    generation: SummaryGeneration


@dataclass(frozen=True)
class MicroCompactionTrace:
    changed: bool
    before_tokens: int
    after_tokens: int
    boundary_message_id: str | None
    before_composition: ContextComposition
    after_composition: ContextComposition
    tool_window: ToolResultWindowStats

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "boundary_message_id": self.boundary_message_id,
            "before_composition": self.before_composition.to_dict(),
            "after_composition": self.after_composition.to_dict(),
            "tool_window": self.tool_window.to_dict(),
        }

    @classmethod
    def from_decision(
        cls,
        decision: MicroCompactionDecision,
    ) -> MicroCompactionTrace:
        return cls(
            changed=decision.changed,
            before_tokens=decision.before.estimated_input_tokens,
            after_tokens=decision.after.estimated_input_tokens,
            boundary_message_id=(
                decision.candidate_state.micro_compacted_through_message_id
            ),
            before_composition=decision.before.composition,
            after_composition=decision.after.composition,
            tool_window=decision.tool_window,
        )


@dataclass(frozen=True)
class PreparedContext:
    model_context: ModelContext
    context_state: ContextState
    micro_compaction: MicroCompactionDecision
    composition: ContextComposition
    summary_compaction: SummaryCompaction | None = None
    blocked_assessment: BudgetAssessment | None = None

    @property
    def micro_compaction_trace(self) -> MicroCompactionTrace:
        return MicroCompactionTrace.from_decision(self.micro_compaction)


class ContextPreparationService:
    """为一次模型调用生成上下文及其候选派生状态。"""

    def __init__(
        self,
        context_manager: ContextManager,
        micro_compaction_policy: MicroCompactionPolicy,
        context_budget: ContextBudget,
        summary_generator: ContextSummaryGenerator,
    ) -> None:
        self.context_manager = context_manager
        self.micro_compaction_policy = micro_compaction_policy
        self.context_budget = context_budget
        self.summary_generator = summary_generator

    def prepare(
        self,
        conversation_records: list[ConversationMessage],
        context_state: ContextState,
        runtime_instructions: list[str],
        tools: list[dict[str, Any]],
        level2_boundary_message_id: str | None = None,
    ) -> PreparedContext:
        decision = self.micro_compaction_policy.propose(
            conversation_records=conversation_records,
            context_state=context_state,
            runtime_instructions=runtime_instructions,
            tools=tools,
        )
        candidate_state = decision.candidate_state
        model_context = self.context_manager.build(
            ContextRequest(
                conversation_records=conversation_records,
                runtime_instructions=runtime_instructions,
                context_state=candidate_state,
            )
        )
        if decision.after.allowed:
            return PreparedContext(
                model_context=model_context,
                context_state=candidate_state,
                micro_compaction=decision,
                composition=decision.after.composition,
            )

        boundary_index = self._eligible_level2_boundary_index(
            conversation_records,
            candidate_state,
            level2_boundary_message_id,
        )
        if boundary_index is None:
            return PreparedContext(
                model_context=model_context,
                context_state=candidate_state,
                micro_compaction=decision,
                composition=decision.after.composition,
                blocked_assessment=decision.after,
            )

        boundary_message_id = conversation_records[boundary_index].message_id
        summary_source_state = replace(
            candidate_state,
            micro_compacted_through_message_id=boundary_message_id,
        )
        summary_source = self.context_manager.build(
            ContextRequest(
                conversation_records=conversation_records[: boundary_index + 1],
                runtime_instructions=[SUMMARY_INSTRUCTION],
                context_state=summary_source_state,
            )
        )
        generation = self.summary_generator.generate(summary_source)
        if isinstance(generation, SummaryGenerationBlocked):
            return PreparedContext(
                model_context=model_context,
                context_state=candidate_state,
                micro_compaction=decision,
                composition=decision.after.composition,
                blocked_assessment=generation.assessment,
            )

        summarized_state = ContextState(
            summary=generation.summary,
            summarized_through_message_id=boundary_message_id,
            micro_compacted_through_message_id=None,
        )
        summarized_context = self.context_manager.build(
            ContextRequest(
                conversation_records=conversation_records,
                runtime_instructions=runtime_instructions,
                context_state=summarized_state,
            )
        )
        after_summary = self.context_budget.assess(summarized_context, tools)
        summary_compaction = SummaryCompaction(
            boundary_message_id=boundary_message_id,
            before=decision.after,
            after=after_summary,
            generation=generation,
        )
        return PreparedContext(
            model_context=summarized_context,
            context_state=(
                summarized_state
                if after_summary.allowed
                else candidate_state
            ),
            micro_compaction=decision,
            composition=after_summary.composition,
            summary_compaction=summary_compaction,
            blocked_assessment=(
                None if after_summary.allowed else after_summary
            ),
        )

    @staticmethod
    def _eligible_level2_boundary_index(
        records: list[ConversationMessage],
        state: ContextState,
        boundary_message_id: str | None,
    ) -> int | None:
        if boundary_message_id is None:
            return None
        indexes = {
            record.message_id: index
            for index, record in enumerate(records)
        }
        boundary_index = indexes[boundary_message_id]
        summary_boundary_id = state.summarized_through_message_id
        summary_index = (
            indexes[summary_boundary_id]
            if summary_boundary_id is not None
            else 0
        )
        if boundary_index <= summary_index:
            return None
        return boundary_index
