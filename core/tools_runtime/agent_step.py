from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core.context import (
    ContextComposition,
    ContextPreparationService,
    ContextState,
    MicroCompactionTrace,
    ModelContext,
    SummaryCompaction,
)
from core.context.preparation import SUMMARY_INSTRUCTION
from core.context.projection import project_system_prompt
from core.messages import ConversationMessage
from core.model_call.client import LLMContextLengthError, LLMTransientError
from core.model_call.service import ModelCallBlocked, ModelCallRequest, ModelCallService
from core.model_call.types import InvalidLLMResponse, LLMResponse, LLMUsage
from core.tools_runtime.tools_checkpoint import (
    Checkpoint,
    context_budget_exceeded_checkpoint,
    context_compressed_checkpoint,
    context_length_exceeded_checkpoint,
    context_prepared_checkpoint,
    invalid_llm_response_checkpoint,
    llm_error_checkpoint,
    llm_request_checkpoint,
    llm_retry_checkpoint,
    llm_usage_checkpoint,
)


@dataclass(frozen=True)
class ModelCallOutcome:
    result: LLMResponse | Checkpoint
    checkpoints: tuple[Checkpoint, ...]


@dataclass(frozen=True)
class AgentStepOutcome:
    result: LLMResponse | Checkpoint
    context_state: ContextState
    compressed: bool
    checkpoints: tuple[Checkpoint, ...]


class AgentStepRunner:
    """完成一次模型交互，但不提交 Conversation 或 Turn Checkpoint。"""

    def __init__(
        self,
        model_calls: ModelCallService,
        model: str,
        context_preparation: ContextPreparationService,
        contextual_user_fragments: list[str] | None = None,
    ) -> None:
        self.model_calls = model_calls
        self.model = model
        self.context_preparation = context_preparation
        self.contextual_user_fragments = contextual_user_fragments or []

    @staticmethod
    def _record_summary_compaction(
        summary_compaction: SummaryCompaction | None,
        checkpoints: list[Checkpoint],
        step_index: int | None,
    ) -> bool:
        if summary_compaction is None:
            return False
        checkpoints.append(
            llm_usage_checkpoint(
                stage="context_summary",
                step_index=step_index,
                usage=LLMUsage(
                    input_tokens=summary_compaction.generation.input_tokens,
                    output_tokens=summary_compaction.generation.output_tokens,
                ),
            )
        )
        checkpoints.append(
            context_compressed_checkpoint(
                boundary_message_id=summary_compaction.boundary_message_id,
                before=summary_compaction.before,
                after=summary_compaction.after,
            )
        )
        return summary_compaction.after.allowed

    @staticmethod
    def _record_context_prepared(
        *,
        stage: str,
        composition: ContextComposition | None,
        micro_compaction_trace: MicroCompactionTrace | None,
        checkpoints: list[Checkpoint],
        step_index: int | None,
    ) -> None:
        if composition is None or micro_compaction_trace is None:
            return
        checkpoints.append(
            context_prepared_checkpoint(
                stage=stage,
                composition=composition,
                micro_compaction=micro_compaction_trace,
                step_index=step_index,
            )
        )

    async def call(
        self,
        model_context: ModelContext,
        tools: list[dict],
        stage: str,
        step_index: int | None,
        runtime_prompts: list[str] | None = None,
        max_llm_retries: int = 3,
    ) -> ModelCallOutcome:
        checkpoints: list[Checkpoint] = []
        last_error = ""
        for attempt in range(1, max_llm_retries + 1):
            checkpoints.append(
                llm_request_checkpoint(
                    stage=stage,
                    step_index=step_index,
                    attempt=attempt,
                    runtime_prompts=runtime_prompts or [],
                    messages=model_context.messages,
                )
            )
            try:
                outcome = await self.model_calls.call(
                    ModelCallRequest(context=model_context, tools=tools),
                    self.model,
                )
                if isinstance(outcome, ModelCallBlocked):
                    result = context_budget_exceeded_checkpoint(
                        stage=stage,
                        step_index=step_index,
                        assessment=outcome.assessment,
                    )
                    return ModelCallOutcome(result, tuple(checkpoints))
                checkpoints.append(
                    llm_usage_checkpoint(
                        stage=stage,
                        step_index=step_index,
                        usage=outcome.usage,
                    )
                )
                return ModelCallOutcome(outcome.response, tuple(checkpoints))
            except InvalidLLMResponse as exc:
                if exc.code == "empty_model_response" and attempt < max_llm_retries:
                    checkpoints.append(
                        llm_retry_checkpoint(
                            stage=stage,
                            step_index=step_index,
                            attempt=attempt,
                            max_attempts=max_llm_retries,
                            error=str(exc),
                        )
                    )
                    await asyncio.sleep(min(attempt, 3))
                    continue
                result = invalid_llm_response_checkpoint(
                    stage=stage,
                    step_index=step_index,
                    reason=exc.code,
                    error=str(exc),
                )
                return ModelCallOutcome(result, tuple(checkpoints))
            except LLMContextLengthError as exc:
                result = context_length_exceeded_checkpoint(
                    stage=stage,
                    step_index=step_index,
                    error=str(exc),
                )
                return ModelCallOutcome(result, tuple(checkpoints))
            except LLMTransientError as exc:
                last_error = str(exc)
                if attempt < max_llm_retries:
                    checkpoints.append(
                        llm_retry_checkpoint(
                            stage=stage,
                            step_index=step_index,
                            attempt=attempt,
                            max_attempts=max_llm_retries,
                            error=last_error,
                        )
                    )
                    await asyncio.sleep(min(attempt, 3))
                    continue
                result = llm_error_checkpoint(
                    stage=stage,
                    step_index=step_index,
                    attempts=max_llm_retries,
                    error=last_error,
                )
                return ModelCallOutcome(result, tuple(checkpoints))

        raise AssertionError("model retry loop terminated without an outcome")

    async def prepare_and_call(
        self,
        *,
        conversation_records: list[ConversationMessage],
        context_state: ContextState,
        runtime_instructions: list[str],
        tools: list[dict],
        level2_boundary_message_id: str | None,
        stage: str,
        step_index: int | None,
        system_prompt_override: str | None = None,
        preparation_failure_stage: str | None = None,
    ) -> AgentStepOutcome:
        checkpoints: list[Checkpoint] = []
        failure_stage = preparation_failure_stage or stage
        records = (
            project_system_prompt(conversation_records, system_prompt_override)
            if system_prompt_override is not None
            else conversation_records
        )
        preparation_prompts = [] if system_prompt_override is not None else runtime_instructions
        request_prompts = (
            [system_prompt_override]
            if system_prompt_override is not None
            else runtime_instructions
        )

        try:
            prepared = await self.context_preparation.prepare(
                conversation_records=records,
                context_state=context_state,
                runtime_instructions=preparation_prompts,
                contextual_user_fragments=self.contextual_user_fragments,
                tools=tools,
                level2_boundary_message_id=level2_boundary_message_id,
                on_summary_request=lambda model_context: checkpoints.append(
                    llm_request_checkpoint(
                        stage="context_summary",
                        step_index=step_index,
                        attempt=1,
                        runtime_prompts=[SUMMARY_INSTRUCTION],
                        messages=model_context.messages,
                    )
                ),
            )
        except LLMContextLengthError as exc:
            result = context_length_exceeded_checkpoint(
                stage=failure_stage,
                step_index=step_index,
                error=str(exc),
            )
            return AgentStepOutcome(result, context_state, False, tuple(checkpoints))
        except LLMTransientError as exc:
            result = llm_error_checkpoint(
                stage=failure_stage,
                step_index=step_index,
                attempts=1,
                error=str(exc),
            )
            return AgentStepOutcome(result, context_state, False, tuple(checkpoints))
        except InvalidLLMResponse as exc:
            result = invalid_llm_response_checkpoint(
                stage=failure_stage,
                step_index=step_index,
                reason=exc.code,
                error=str(exc),
            )
            return AgentStepOutcome(result, context_state, False, tuple(checkpoints))

        compressed = self._record_summary_compaction(
            prepared.summary_compaction,
            checkpoints,
            step_index,
        )
        self._record_context_prepared(
            stage=stage,
            composition=prepared.composition,
            micro_compaction_trace=prepared.micro_compaction_trace,
            checkpoints=checkpoints,
            step_index=step_index,
        )
        if prepared.blocked_assessment is not None:
            result = context_budget_exceeded_checkpoint(
                stage=failure_stage,
                step_index=step_index,
                assessment=prepared.blocked_assessment,
            )
            return AgentStepOutcome(
                result,
                prepared.context_state,
                compressed,
                tuple(checkpoints),
            )

        call_outcome = await self.call(
            prepared.model_context,
            tools,
            stage,
            step_index,
            runtime_prompts=request_prompts,
        )
        checkpoints.extend(call_outcome.checkpoints)
        return AgentStepOutcome(
            call_outcome.result,
            prepared.context_state,
            compressed,
            tuple(checkpoints),
        )
