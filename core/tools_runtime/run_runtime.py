from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from core.tools_runtime.tools_checkpoint import (
    Checkpoint,
    budget_stop_checkpoint,
    context_length_exceeded_checkpoint,
    format_checkpoint,
    invalid_llm_response_checkpoint,
    llm_error_checkpoint,
    llm_retry_checkpoint,
    message_chain_invalid_checkpoint,
    run_completed_checkpoint,
    run_interrupted_checkpoint,
    run_started_checkpoint,
    tool_batch_completed_checkpoint,
    verification_required_checkpoint,
)
from core.messages import Conversation, InvalidLLMResponse, LLMResponse
from core.llm_client import (
    LLMClient,
    LLMContextLengthError,
    LLMTransientError,
)
from core.tool_registry import get_tools
from core.tools_runtime.stop_guard import evaluate_stop_safety
from core.tools_runtime.tools_executor import encode_tool_result, execute_tool
from core.tools_runtime.tools_protocol import (
    build_tool_messages,
    validate_tool_message_chain,
)
from core.tools_runtime.tools_state import ToolsState
from core.runtime_modes import RuntimeMode
from core.context_manager import ContextManager, ModelContext, ContextRequest

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

    @property
    def final_reason(self) -> str | None:
        if self.status == RunStatus.COMPLETED or not self.checkpoints:
            return None
        return self.checkpoints[-1].reason


class RunRuntime:
    """最小 tool-calling 运行内核。"""

    def __init__(
        self,
        llm_client: LLMClient,
        model: str,
        runtime_mode: RuntimeMode,
        context_manager: ContextManager,
    ):
        self.llm_client = llm_client
        self.model = model
        self.runtime_mode = runtime_mode
        self.context_manager = context_manager

    @staticmethod
    def _finish(
        *,
        status: RunStatus,
        answer: str,
        checkpoint: Checkpoint,
        checkpoints: list[Checkpoint],
    ) -> RunResult:
        checkpoints.append(checkpoint)
        return RunResult(
            status=status,
            answer=answer,
            checkpoints=checkpoints,
        )

    def _call_llm_with_retry(
        self,
        model_context: ModelContext,
        round_index: int,
        checkpoints: list[Checkpoint],
        max_llm_retries: int = 3,
    ) -> LLMResponse | Checkpoint:
        last_error = ""
        for attempt in range(1, max_llm_retries + 1):
            try:
                return self.llm_client.chat(
                    model_context.messages,
                    self.model,
                    get_tools(),
                )
            except InvalidLLMResponse as exc:
                return invalid_llm_response_checkpoint(
                    round_index=round_index,
                    reason=exc.code,
                    error=str(exc),
                )
            except LLMContextLengthError as exc:
                return context_length_exceeded_checkpoint(
                    round_index=round_index,
                    error=str(exc),
                )
            except LLMTransientError as exc:
                last_error = str(exc)
                if attempt < max_llm_retries:
                    checkpoints.append(
                        llm_retry_checkpoint(
                            round_index=round_index,
                            attempt=attempt,
                            max_attempts=max_llm_retries,
                            error=last_error,
                        )
                    )
                    time.sleep(min(attempt, 3))  # 1s, 2s, 3s...
                    continue

                checkpoint = llm_error_checkpoint(
                    round_index=round_index,
                    attempts=max_llm_retries,
                    error=last_error,
                )
                return checkpoint
    def run(
        self,
        conversation: Conversation,
        user_message: str,
        max_rounds: int = 20,
        control: RunControl | None = None,
    ) -> RunResult:
        checkpoints: list[Checkpoint] = []
        tools_state = ToolsState()
        run_control = control or RunControl()
        self.runtime_mode.start(user_message, conversation, self.llm_client, self.model)

        checkpoints.append(run_started_checkpoint(max_rounds))
        conversation.add_user(user_message)

        for round_index in range(1, max_rounds + 1):
            validation = validate_tool_message_chain(conversation.messages)
            if not validation.ok:
                checkpoint = message_chain_invalid_checkpoint(validation.to_dict())
                return self._finish(
                    status=RunStatus.FAILED,
                    answer=format_checkpoint(checkpoint),
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                )
            context = self.context_manager.build(
                ContextRequest(
                    conversation_messages=conversation.messages,
                    runtime_instructions=self.runtime_mode.runtime_instructions(),
                )
            )
            llm_outcome = self._call_llm_with_retry(
                context,
                round_index,
                checkpoints,
            )
            if isinstance(llm_outcome, Checkpoint):
                status = (
                    RunStatus.BLOCKED
                    if llm_outcome.reason == "context_length_exceeded"
                    else RunStatus.FAILED
                )
                return self._finish(
                    status=status,
                    answer=format_checkpoint(llm_outcome),
                    checkpoint=llm_outcome,
                    checkpoints=checkpoints,
                )
            response = llm_outcome

            conversation.add_assistant(response)
            if response.type == "text":
                if self.runtime_mode.on_assistant_text(conversation):
                    continue

                stop_safety = evaluate_stop_safety(
                    conversation.messages,
                    tools_state,
                )
                if not stop_safety.protocol_safe:
                    validation = validate_tool_message_chain(conversation.messages)
                    checkpoint = message_chain_invalid_checkpoint(validation.to_dict())
                    return self._finish(
                        status=RunStatus.FAILED,
                        answer=format_checkpoint(checkpoint),
                        checkpoint=checkpoint,
                        checkpoints=checkpoints,
                    )

                if not stop_safety.business_safe:
                    checkpoint = verification_required_checkpoint()
                    checkpoints.append(checkpoint)
                    conversation.add_user(checkpoint.message)
                    continue

                checkpoint = run_completed_checkpoint(
                    answer=response.content,
                    extra_data=self.runtime_mode.checkpoint_data(),
                )
                return self._finish(
                    status=RunStatus.COMPLETED,
                    answer=response.content,
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                )

            calls = response.calls
            batch_steps = tools_state.add_calls(calls)

            for call in calls:
                tool_result = execute_tool(call.name, call.arguments)
                tools_state.add_result(call.id, tool_result)

            tool_results = build_tool_messages(batch_steps, encode_tool_result)
            conversation.add_tools_result(tool_results)
            self.runtime_mode.after_tool_batch(
                conversation,
                tools_state,
                batch_steps,
            )
            checkpoints.append(
                tool_batch_completed_checkpoint(
                    round_index,
                    tools_state,
                    len(calls),
                    self.runtime_mode.checkpoint_data(),
                )
            )

            if run_control.interrupt_requested:
                stop_safety = evaluate_stop_safety(
                    conversation.messages,
                    tools_state,
                )
                if not stop_safety.protocol_safe:
                    validation = validate_tool_message_chain(conversation.messages)
                    checkpoint = message_chain_invalid_checkpoint(validation.to_dict())
                    return self._finish(
                        status=RunStatus.FAILED,
                        answer=format_checkpoint(checkpoint),
                        checkpoint=checkpoint,
                        checkpoints=checkpoints,
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
                )

        checkpoint = budget_stop_checkpoint(max_rounds, tools_state)
        return self._finish(
            status=RunStatus.BLOCKED,
            answer=format_checkpoint(checkpoint),
            checkpoint=checkpoint,
            checkpoints=checkpoints,
        )
