from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from core.context.budget import BudgetAssessment, ContextBudget
from core.context.manager import ContextManager, ContextRequest, ModelContext
from core.context.state import ContextState
from core.messages import ConversationMessage


@dataclass(frozen=True)
class MicroCompactionConfig:
    trigger_ratio: float
    target_ratio: float
    recent_protection_tokens: int

    def __post_init__(self) -> None:
        if not 0 < self.trigger_ratio < 1:
            raise ValueError("trigger_ratio 必须在 0 和 1 之间")
        if not 0 < self.target_ratio < self.trigger_ratio:
            raise ValueError("target_ratio 必须大于 0 且小于 trigger_ratio")
        if self.recent_protection_tokens <= 0:
            raise ValueError("recent_protection_tokens 必须大于 0")


@dataclass(frozen=True)
class MicroCompactionDecision:
    candidate_state: ContextState
    before: BudgetAssessment
    after: BudgetAssessment
    changed: bool


class MicroCompactionPolicy:
    def __init__(
        self,
        context_manager: ContextManager,
        context_budget: ContextBudget,
        config: MicroCompactionConfig,
    ) -> None:
        self.context_manager = context_manager
        self.context_budget = context_budget
        self.config = config

    def propose(
        self,
        conversation_records: list[ConversationMessage],
        context_state: ContextState,
        runtime_instructions: list[str],
        tools: list[dict[str, Any]],
    ) -> MicroCompactionDecision:
        before = self._assess(
            conversation_records,
            context_state,
            runtime_instructions,
            tools,
        )
        trigger_tokens = int(
            before.input_budget_tokens * self.config.trigger_ratio
        )
        if before.estimated_input_tokens < trigger_tokens:
            return MicroCompactionDecision(
                candidate_state=context_state,
                before=before,
                after=before,
                changed=False,
            )

        record_indexes = {
            record.message_id: index
            for index, record in enumerate(conversation_records)
        }
        summary_index = self._state_boundary_index(
            context_state.summarized_through_message_id,
            record_indexes,
        )
        current_micro_index = self._state_boundary_index(
            context_state.micro_compacted_through_message_id,
            record_indexes,
        )
        minimum_recent_index = max(1, summary_index + 1)
        recent_start_index = self._recent_start_index(
            conversation_records,
            minimum_recent_index,
        )
        max_boundary_index = recent_start_index - 1
        first_candidate_index = max(
            1,
            summary_index + 1,
            current_micro_index + 1,
        )
        if first_candidate_index > max_boundary_index:
            return MicroCompactionDecision(
                candidate_state=context_state,
                before=before,
                after=before,
                changed=False,
            )

        target_tokens = int(
            before.input_budget_tokens * self.config.target_ratio
        )
        best_state = context_state
        best_assessment = before

        for boundary_index in range(
            first_candidate_index,
            max_boundary_index + 1,
        ):
            candidate_state = replace(
                context_state,
                micro_compacted_through_message_id=(
                    conversation_records[boundary_index].message_id
                ),
            )
            assessment = self._assess(
                conversation_records,
                candidate_state,
                runtime_instructions,
                tools,
            )
            if (
                assessment.estimated_input_tokens
                < best_assessment.estimated_input_tokens
            ):
                best_state = candidate_state
                best_assessment = assessment
            if assessment.estimated_input_tokens <= target_tokens:
                return MicroCompactionDecision(
                    candidate_state=candidate_state,
                    before=before,
                    after=assessment,
                    changed=True,
                )

        return MicroCompactionDecision(
            candidate_state=best_state,
            before=before,
            after=best_assessment,
            changed=best_state != context_state,
        )

    def _assess(
        self,
        records: list[ConversationMessage],
        state: ContextState,
        runtime_instructions: list[str],
        tools: list[dict[str, Any]],
    ) -> BudgetAssessment:
        context = self.context_manager.build(
            ContextRequest(
                conversation_records=records,
                runtime_instructions=runtime_instructions,
                context_state=state,
            )
        )
        return self.context_budget.assess(context, tools)

    def _recent_start_index(
        self,
        records: list[ConversationMessage],
        minimum_index: int,
    ) -> int:
        start_index = len(records)
        for index in range(len(records) - 1, minimum_index - 1, -1):
            start_index = index
            recent_context = ModelContext(
                messages=[
                    record.payload
                    for record in records[start_index:]
                ]
            )
            recent_tokens = self.context_budget.estimator.estimate(
                recent_context,
                [],
            )
            if recent_tokens >= self.config.recent_protection_tokens:
                break
        return start_index

    @staticmethod
    def _state_boundary_index(
        message_id: str | None,
        record_indexes: dict[str, int],
    ) -> int:
        if message_id is None:
            return 0
        return record_indexes[message_id]
