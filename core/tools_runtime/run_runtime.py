from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from core.tools_runtime.tools_checkpoint import (
    Checkpoint,
    budget_stop_checkpoint,
    context_budget_exceeded_checkpoint,
    context_compressed_checkpoint,
    context_length_exceeded_checkpoint,
    context_prepared_checkpoint,
    format_checkpoint,
    invalid_llm_response_checkpoint,
    llm_error_checkpoint,
    llm_retry_checkpoint,
    llm_usage_checkpoint,
    message_chain_invalid_checkpoint,
    todo_list_created_checkpoint,
    run_completed_checkpoint,
    run_interrupted_checkpoint,
    run_started_checkpoint,
    tool_batch_completed_checkpoint,
    verification_required_checkpoint,
)
from core.messages import Conversation
from core.model_call.client import LLMContextLengthError, LLMTransientError
from core.model_call.service import (
    ModelCallBlocked,
    ModelCallRequest,
    ModelCallService,
)
from core.model_call.types import InvalidLLMResponse, LLMResponse, LLMUsage
from core.context.projection import project_system_prompt
from core.tools_runtime.stop_guard import evaluate_stop_safety
from core.tools_runtime.tools_executor import ToolsExecutor, encode_tool_result
from core.tools_runtime.tools_protocol import (
    build_tool_messages,
    validate_tool_message_chain,
)
from core.tools_runtime.tools_state import ToolsState
from core.runtime_modes import RuntimeMode
from core.context import (
    ContextComposition,
    ContextPreparationService,
    ContextState,
    MicroCompactionTrace,
    ModelContext,
    SummaryCompaction,
)
from core.runtime_artifacts import ToolResultExternalizer

class RunStatus(str, Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class RunControl:
    interrupt_requested: bool = False
    interrupt_reason: str | None = None

    def request_interrupt(self, reason: str | None = None) -> None:
        self.interrupt_requested = True
        self.interrupt_reason = reason


@dataclass
class RunResult:
    status: RunStatus
    answer: str
    checkpoints: list[Checkpoint]
    context_state: ContextState

    @property
    def final_reason(self) -> str | None:
        if self.status == RunStatus.COMPLETED or not self.checkpoints:
            return None
        return self.checkpoints[-1].reason


class RunRuntime:
    """最小 tool-calling 运行内核。"""

    def __init__(
        self,
        model_calls: ModelCallService,
        model: str,
        runtime_mode: RuntimeMode,
        context_preparation: ContextPreparationService,
        tools_executor: ToolsExecutor,
        tool_result_externalizer: ToolResultExternalizer,
    ):
        self.model_calls = model_calls
        self.model = model
        self.runtime_mode = runtime_mode
        self.context_preparation = context_preparation
        self.tools_executor = tools_executor
        self.tool_result_externalizer = tool_result_externalizer

    @staticmethod
    def _record_summary_compaction(
        summary_compaction: SummaryCompaction | None,
        checkpoints: list[Checkpoint],
        round_index: int | None = None,
    ) -> bool:
        if summary_compaction is None:
            return False
        checkpoints.append(
            llm_usage_checkpoint(
                stage="context_summary",
                round_index=round_index,
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
        round_index: int | None = None,
    ) -> None:
        if composition is None or micro_compaction_trace is None:
            return
        checkpoints.append(
            context_prepared_checkpoint(
                stage=stage,
                composition=composition,
                micro_compaction=micro_compaction_trace,
                round_index=round_index,
            )
        )

    @staticmethod
    def _finish(
        *,
        status: RunStatus,
        answer: str,
        checkpoint: Checkpoint,
        checkpoints: list[Checkpoint],
        context_state: ContextState,
    ) -> RunResult:
        checkpoints.append(checkpoint)
        return RunResult(
            status=status,
            answer=answer,
            checkpoints=checkpoints,
            context_state=context_state,
        )

    def _call_llm_with_retry(
        self,
        model_context: ModelContext,
        tools: list[dict],
        stage: str,
        round_index: int | None,
        checkpoints: list[Checkpoint],
        max_llm_retries: int = 3,
    ) -> LLMResponse | Checkpoint:
        last_error = ""
        for attempt in range(1, max_llm_retries + 1):
            try:
                outcome = self.model_calls.call(
                    ModelCallRequest(
                        context=model_context,
                        tools=tools,
                    ),
                    self.model,
                )
                if isinstance(outcome, ModelCallBlocked):
                    return context_budget_exceeded_checkpoint(
                        stage=stage,
                        round_index=round_index,
                        assessment=outcome.assessment,
                    )
                checkpoints.append(
                    llm_usage_checkpoint(
                        stage=stage,
                        round_index=round_index,
                        usage=outcome.usage,
                    )
                )
                return outcome.response
            except InvalidLLMResponse as exc:
                if exc.code == "empty_model_response" and attempt < max_llm_retries:
                    checkpoints.append(
                        llm_retry_checkpoint(
                            stage=stage,
                            round_index=round_index,
                            attempt=attempt,
                            max_attempts=max_llm_retries,
                            error=str(exc),
                        )
                    )
                    time.sleep(min(attempt, 3))
                    continue
                return invalid_llm_response_checkpoint(
                    stage=stage,
                    round_index=round_index,
                    reason=exc.code,
                    error=str(exc),
                )
            except LLMContextLengthError as exc:
                return context_length_exceeded_checkpoint(
                    stage=stage,
                    round_index=round_index,
                    error=str(exc),
                )
            except LLMTransientError as exc:
                last_error = str(exc)
                if attempt < max_llm_retries:
                    checkpoints.append(
                        llm_retry_checkpoint(
                            stage=stage,
                            round_index=round_index,
                            attempt=attempt,
                            max_attempts=max_llm_retries,
                            error=last_error,
                        )
                    )
                    time.sleep(min(attempt, 3))  # 1s, 2s, 3s...
                    continue

                checkpoint = llm_error_checkpoint(
                    stage=stage,
                    round_index=round_index,
                    attempts=max_llm_retries,
                    error=last_error,
                )
                return checkpoint

    def _prepare_and_call_role(
        self,
        *,
        conversation: Conversation,
        system_prompt: str,
        context_state: ContextState,
        level2_boundary_message_id: str | None,
        stage: str,
        round_index: int | None,
        checkpoints: list[Checkpoint],
        tools: list[dict],
    ) -> tuple[LLMResponse | Checkpoint, ContextState, bool]:
        try:
            prepared = self.context_preparation.prepare(
                conversation_records=project_system_prompt(
                    conversation.records,
                    system_prompt,
                ),
                context_state=context_state,
                runtime_instructions=[],
                tools=tools,
                level2_boundary_message_id=level2_boundary_message_id,
            )
        except LLMContextLengthError as exc:
            return (
                context_length_exceeded_checkpoint(
                    stage=stage,
                    round_index=round_index,
                    error=str(exc),
                ),
                context_state,
                False,
            )
        except LLMTransientError as exc:
            return (
                llm_error_checkpoint(
                    stage=stage,
                    round_index=round_index,
                    attempts=1,
                    error=str(exc),
                ),
                context_state,
                False,
            )
        except InvalidLLMResponse as exc:
            return (
                invalid_llm_response_checkpoint(
                    stage=stage,
                    round_index=round_index,
                    reason=exc.code,
                    error=str(exc),
                ),
                context_state,
                False,
            )

        compressed = self._record_summary_compaction(
            prepared.summary_compaction,
            checkpoints,
            round_index,
        )
        self._record_context_prepared(
            stage=stage,
            composition=prepared.composition,
            micro_compaction_trace=prepared.micro_compaction_trace,
            checkpoints=checkpoints,
            round_index=round_index,
        )
        if prepared.blocked_assessment is not None:
            return (
                context_budget_exceeded_checkpoint(
                    stage=stage,
                    round_index=round_index,
                    assessment=prepared.blocked_assessment,
                ),
                prepared.context_state,
                compressed,
            )

        response = self._call_llm_with_retry(
            prepared.model_context,
            tools,
            stage,
            round_index,
            checkpoints,
        )
        return response, prepared.context_state, compressed

    def run(
        self,
        conversation: Conversation,
        user_message: str,
        max_rounds: int = 20,
        control: RunControl | None = None,
        context_state: ContextState | None = None,
    ) -> RunResult:
        checkpoints: list[Checkpoint] = []
        tools_state = ToolsState()
        mode_state = self.runtime_mode.create_state()
        run_control = control or RunControl()
        current_context_state = context_state or ContextState()
        level2_performed = False
        level2_boundary_message_id = (
            conversation.records[-1].message_id
            if conversation.records
            else None
        )
        checkpoints.append(run_started_checkpoint(max_rounds))
        conversation.add_user(user_message)
        start_prompt = self.runtime_mode.start(mode_state)
        if start_prompt is not None:
            start_tools = self.runtime_mode.runtime_tools(mode_state)
            start_response, current_context_state, compressed = (
                self._prepare_and_call_role(
                    conversation=conversation,
                    system_prompt=start_prompt,
                    context_state=current_context_state,
                    level2_boundary_message_id=level2_boundary_message_id,
                    stage="todo_initialization",
                    round_index=None,
                    checkpoints=checkpoints,
                    tools=start_tools,
                )
            )
            level2_performed = level2_performed or compressed
            if isinstance(start_response, Checkpoint):
                status = (
                    RunStatus.BLOCKED
                    if start_response.reason in {
                        "context_budget_exceeded",
                        "context_length_exceeded",
                    }
                    else RunStatus.FAILED
                )
                return self._finish(
                    status=status,
                    answer=format_checkpoint(start_response),
                    checkpoint=start_response,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )
            try:
                start_data = self.runtime_mode.accept_start_response(
                    mode_state,
                    start_response
                )
            except InvalidLLMResponse as exc:
                checkpoint = invalid_llm_response_checkpoint(
                    stage="todo_initialization",
                    round_index=None,
                    reason=exc.code,
                    error=str(exc),
                )
                return self._finish(
                    status=RunStatus.FAILED,
                    answer=format_checkpoint(checkpoint),
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )
            if start_data is not None:
                checkpoints.append(todo_list_created_checkpoint(start_data))
        external_tools = self.tools_executor.registry.get_tools()
        runtime_tools = self.runtime_mode.runtime_tools(mode_state)
        external_names = {
            tool["function"]["name"] for tool in external_tools
        }
        runtime_names = {
            tool["function"]["name"] for tool in runtime_tools
        }
        duplicated_names = external_names & runtime_names
        if duplicated_names:
            raise ValueError(
                f"runtime tool conflicts with external tool: {sorted(duplicated_names)}"
            )
        tools = external_tools + runtime_tools

        for round_index in range(1, max_rounds + 1):
            validation = validate_tool_message_chain(
                conversation.protocol_messages()
            )
            if not validation.ok:
                checkpoint = message_chain_invalid_checkpoint(validation.to_dict())
                return self._finish(
                    status=RunStatus.FAILED,
                    answer=format_checkpoint(checkpoint),
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )
            try:
                prepared = self.context_preparation.prepare(
                    conversation_records=conversation.records,
                    context_state=current_context_state,
                    runtime_instructions=self.runtime_mode.runtime_instructions(
                        mode_state
                    ),
                    tools=tools,
                    level2_boundary_message_id=level2_boundary_message_id,
                )
            except LLMContextLengthError as exc:
                checkpoint = context_length_exceeded_checkpoint(
                    stage="context_summary",
                    round_index=round_index,
                    error=str(exc),
                )
                return self._finish(
                    status=RunStatus.BLOCKED,
                    answer=format_checkpoint(checkpoint),
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )
            except LLMTransientError as exc:
                checkpoint = llm_error_checkpoint(
                    stage="context_summary",
                    round_index=round_index,
                    attempts=1,
                    error=str(exc),
                )
                return self._finish(
                    status=RunStatus.FAILED,
                    answer=format_checkpoint(checkpoint),
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )
            except InvalidLLMResponse as exc:
                checkpoint = invalid_llm_response_checkpoint(
                    stage="context_summary",
                    round_index=round_index,
                    reason=exc.code,
                    error=str(exc),
                )
                return self._finish(
                    status=RunStatus.FAILED,
                    answer=format_checkpoint(checkpoint),
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )
            current_context_state = prepared.context_state
            if self._record_summary_compaction(
                prepared.summary_compaction,
                checkpoints,
                round_index,
            ):
                level2_performed = True
            self._record_context_prepared(
                stage="agent_round",
                composition=prepared.composition,
                micro_compaction_trace=prepared.micro_compaction_trace,
                checkpoints=checkpoints,
                round_index=round_index,
            )
            if prepared.blocked_assessment is not None:
                checkpoint = context_budget_exceeded_checkpoint(
                    stage="context_summary",
                    round_index=round_index,
                    assessment=prepared.blocked_assessment,
                )
                return self._finish(
                    status=RunStatus.BLOCKED,
                    answer=format_checkpoint(checkpoint),
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )
            llm_outcome = self._call_llm_with_retry(
                prepared.model_context,
                tools,
                "agent_round",
                round_index,
                checkpoints,
            )
            if isinstance(llm_outcome, Checkpoint):
                status = (
                    RunStatus.BLOCKED
                    if llm_outcome.reason in {
                        "context_budget_exceeded",
                        "context_length_exceeded",
                    }
                    else RunStatus.FAILED
                )
                return self._finish(
                    status=status,
                    answer=format_checkpoint(llm_outcome),
                    checkpoint=llm_outcome,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )
            response = llm_outcome

            conversation.add_assistant(response)
            if response.type == "text":
                final_feedback = self.runtime_mode.check_final_candidate(
                    mode_state
                )
                if final_feedback is not None:
                    conversation.add_user(final_feedback)
                    continue

                stop_safety = evaluate_stop_safety(
                    conversation.protocol_messages(),
                    tools_state,
                )
                if not stop_safety.protocol_safe:
                    validation = validate_tool_message_chain(
                        conversation.protocol_messages()
                    )
                    checkpoint = message_chain_invalid_checkpoint(validation.to_dict())
                    return self._finish(
                        status=RunStatus.FAILED,
                        answer=format_checkpoint(checkpoint),
                        checkpoint=checkpoint,
                        checkpoints=checkpoints,
                        context_state=current_context_state,
                    )

                if not stop_safety.business_safe:
                    checkpoint = verification_required_checkpoint()
                    checkpoints.append(checkpoint)
                    conversation.add_user(checkpoint.message)
                    continue

                answer = response.content
                if level2_performed:
                    answer = "本轮已执行上下文压缩。\n\n" + answer
                self.runtime_mode.on_run_completed(mode_state)
                checkpoint = run_completed_checkpoint(
                    answer=answer,
                    extra_data=self.runtime_mode.checkpoint_data(mode_state),
                )
                return self._finish(
                    status=RunStatus.COMPLETED,
                    answer=answer,
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )

            calls = response.calls
            batch_steps = tools_state.add_calls(calls)
            result_chars_before = 0
            result_chars_after = 0
            externalized_count = 0

            for call in calls:
                if self.runtime_mode.handles_tool(call.name):
                    tool_result = self.runtime_mode.execute_tool(
                        mode_state,
                        call.name,
                        call.arguments,
                    )
                else:
                    tool_result = self.tools_executor.execute(
                        call.name,
                        call.arguments,
                    )
                outcome = self.tool_result_externalizer.process(tool_result)
                result_chars_before += outcome.original_chars
                result_chars_after += outcome.projected_chars
                if outcome.externalized:
                    externalized_count += 1
                tools_state.add_result(call.id, outcome.result)

            tool_results = build_tool_messages(batch_steps, encode_tool_result)
            conversation.add_tools_result(tool_results)
            batch_feedback = self.runtime_mode.after_tool_batch(
                mode_state,
                batch_steps,
            )
            if batch_feedback is not None:
                conversation.add_user(batch_feedback)
            checkpoints.append(
                tool_batch_completed_checkpoint(
                    round_index,
                    tools_state,
                    len(calls),
                    self.runtime_mode.checkpoint_data(mode_state),
                    result_chars_before=result_chars_before,
                    result_chars_after=result_chars_after,
                    externalized_count=externalized_count,
                )
            )

            if run_control.interrupt_requested:
                stop_safety = evaluate_stop_safety(
                    conversation.protocol_messages(),
                    tools_state,
                )
                if not stop_safety.protocol_safe:
                    validation = validate_tool_message_chain(
                        conversation.protocol_messages()
                    )
                    checkpoint = message_chain_invalid_checkpoint(validation.to_dict())
                    return self._finish(
                        status=RunStatus.FAILED,
                        answer=format_checkpoint(checkpoint),
                        checkpoint=checkpoint,
                        checkpoints=checkpoints,
                        context_state=current_context_state,
                    )

                if not stop_safety.business_safe:
                    checkpoint = verification_required_checkpoint()
                    checkpoints.append(checkpoint)
                    conversation.add_user(checkpoint.message)
                    continue

                checkpoint = run_interrupted_checkpoint(
                    run_control.interrupt_reason
                )
                return self._finish(
                    status=RunStatus.INTERRUPTED,
                    answer=checkpoint.message,
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                )

        checkpoint = budget_stop_checkpoint(max_rounds, tools_state)
        return self._finish(
            status=RunStatus.BLOCKED,
            answer=format_checkpoint(checkpoint),
            checkpoint=checkpoint,
            checkpoints=checkpoints,
            context_state=current_context_state,
        )
