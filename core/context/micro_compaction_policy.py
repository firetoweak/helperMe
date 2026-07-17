from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from typing import Any

from core.context.budget import BudgetAssessment, ContextBudget
from core.context.composition import (
    ToolResultWindowStats,
    content_char_length,
    parse_tool_result_meta,
)
from core.context.manager import ContextManager, ContextRequest, ModelContext
from core.context.state import ContextState
from core.messages import ConversationMessage
from core.runtime_artifacts.store import ArtifactStore


@dataclass(frozen=True)
class MicroCompactionConfig:
    recent_protection_tokens: int

    def __post_init__(self) -> None:
        if self.recent_protection_tokens <= 0:
            raise ValueError("recent_protection_tokens 必须大于 0")


@dataclass(frozen=True)
class MicroCompactionDecision:
    candidate_state: ContextState
    before: BudgetAssessment
    after: BudgetAssessment
    changed: bool
    tool_window: ToolResultWindowStats
    newly_dehydrated_message_ids: tuple[str, ...] = ()


class MicroCompactionPolicy:
    """持续性 Level 1：recent 窗外已消费成功 tool body 懒落盘并脱水投影。"""

    def __init__(
        self,
        context_manager: ContextManager,
        context_budget: ContextBudget,
        config: MicroCompactionConfig,
        artifact_store: ArtifactStore,
    ) -> None:
        self.context_manager = context_manager
        self.context_budget = context_budget
        self.config = config
        self.artifact_store = artifact_store

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
        record_indexes = {
            record.message_id: index
            for index, record in enumerate(conversation_records)
        }
        summary_index = self._state_boundary_index(
            context_state.summarized_through_message_id,
            record_indexes,
        )
        minimum_recent_index = max(1, summary_index + 1)
        recent_start_index = self._recent_start_index(
            conversation_records,
            minimum_recent_index,
        )
        tool_window = self._tool_window_stats(
            conversation_records,
            recent_start_index,
        )

        eligible_ids = self._eligible_tool_message_ids(
            conversation_records,
            max_index_exclusive=recent_start_index,
        )
        new_artifacts = dict(context_state.tool_artifacts)
        newly_dehydrated: list[str] = []
        for message_id in eligible_ids:
            if message_id in new_artifacts:
                continue
            record = conversation_records[record_indexes[message_id]]
            content = record.payload.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(
                    content,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            _, existing_artifact_id = parse_tool_result_meta(content)
            if existing_artifact_id is not None:
                artifact_id = existing_artifact_id
            else:
                artifact_id = self.artifact_store.save(content).artifact_id
            new_artifacts[message_id] = artifact_id
            newly_dehydrated.append(message_id)

        candidate_state = replace(
            context_state,
            tool_artifacts=new_artifacts,
        )
        changed = bool(newly_dehydrated)
        after = (
            before
            if not changed
            else self._assess(
                conversation_records,
                candidate_state,
                runtime_instructions,
                tools,
            )
        )

        return MicroCompactionDecision(
            candidate_state=candidate_state,
            before=before,
            after=after,
            changed=changed,
            tool_window=tool_window,
            newly_dehydrated_message_ids=tuple(newly_dehydrated),
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

    def _eligible_tool_message_ids(
        self,
        records: list[ConversationMessage],
        *,
        max_index_exclusive: int,
    ) -> list[str]:
        """返回可脱水的 tool message_id（批次完整、成功、已消费，且全在保护窗外）。"""
        payloads = [record.payload for record in records]
        eligible: list[str] = []
        index = 0
        while index < len(payloads):
            message = payloads[index]
            calls = message.get("tool_calls")
            if (
                index >= max_index_exclusive
                or message.get("role") != "assistant"
                or not calls
            ):
                index += 1
                continue

            result_end = index + 1
            while (
                result_end < len(payloads)
                and payloads[result_end].get("role") == "tool"
            ):
                result_end += 1

            results = payloads[index + 1 : result_end]
            call_ids = [call["id"] for call in calls]
            result_ids = [result.get("tool_call_id") for result in results]
            batch_is_complete = (
                result_ids == call_ids
                and result_end - 1 < max_index_exclusive
            )
            if not batch_is_complete:
                index += 1
                continue

            batch_was_consumed = any(
                later.get("role") == "assistant"
                for later in payloads[result_end:max_index_exclusive]
            )
            try:
                batch_succeeded = all(
                    json.loads(result["content"])["ok"] is True
                    for result in results
                )
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                batch_succeeded = False

            if batch_was_consumed and batch_succeeded:
                for tool_index in range(index + 1, result_end):
                    eligible.append(records[tool_index].message_id)

            index = result_end

        return eligible

    def _tool_window_stats(
        self,
        records: list[ConversationMessage],
        recent_start_index: int,
    ) -> ToolResultWindowStats:
        recent_records = records[recent_start_index:]
        compressible_records = records[:recent_start_index]
        recent_start_message_id = None
        if 0 <= recent_start_index < len(records):
            recent_start_message_id = records[recent_start_index].message_id

        return ToolResultWindowStats(
            recent_start_message_id=recent_start_message_id,
            recent_protection_tokens=self.config.recent_protection_tokens,
            recent_tool_chars=self._tool_chars(recent_records),
            compressible_tool_chars=self._tool_chars(compressible_records),
            recent_tool_tokens_estimate=self._estimate_tool_tokens(
                recent_records
            ),
            compressible_tool_tokens_estimate=self._estimate_tool_tokens(
                compressible_records
            ),
        )

    @staticmethod
    def _tool_chars(records: list[ConversationMessage]) -> int:
        total = 0
        for record in records:
            if record.payload.get("role") != "tool":
                continue
            total += content_char_length(record.payload.get("content", ""))
        return total

    def _estimate_tool_tokens(
        self,
        records: list[ConversationMessage],
    ) -> int:
        tool_messages = [
            deepcopy(record.payload)
            for record in records
            if record.payload.get("role") == "tool"
        ]
        if not tool_messages:
            return 0
        return self.context_budget.estimator.estimate(
            ModelContext(messages=tool_messages),
            [],
        )

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
