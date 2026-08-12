from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.context import ContextState, ModelContext
from core.messages import Conversation
from core.model_call.types import InvalidLLMResponse
from core.runtime_modes import RunMode, RuntimeMode, RuntimeModeRouter
from core.tools_runtime.model_turn import ModelTurnRunner
from core.tools_runtime.tools_checkpoint import (
    Checkpoint,
    invalid_llm_response_checkpoint,
    runtime_mode_activation_failed_checkpoint,
    runtime_mode_fallback_checkpoint,
    runtime_mode_routed_checkpoint,
    todo_list_created_checkpoint,
)


@dataclass(frozen=True)
class ModeActivationOutcome:
    runtime_mode: RuntimeMode | None
    mode_state: Any
    context_state: ContextState
    compressed: bool
    checkpoints: tuple[Checkpoint, ...]
    terminal_checkpoint: Checkpoint | None = None


class ModeActivator:
    """选择并初始化一次 Run 使用的 RuntimeMode。"""

    def __init__(
        self,
        *,
        model_turn_runner: ModelTurnRunner,
        default_mode: RuntimeMode | None,
        mode_router: RuntimeModeRouter | None,
        runtime_modes: dict[RunMode, RuntimeMode] | None,
    ) -> None:
        self.model_turn_runner = model_turn_runner
        self.default_mode = default_mode
        self.mode_router = mode_router
        self.runtime_modes = runtime_modes

    async def activate(
        self,
        *,
        conversation: Conversation,
        requested_mode: RuntimeMode | None,
        context_state: ContextState,
        level2_boundary_message_id: str | None,
    ) -> ModeActivationOutcome:
        checkpoints: list[Checkpoint] = []
        runtime_mode = requested_mode or self.default_mode

        if requested_mode is None and self.mode_router is not None:
            route_outcome = await self.model_turn_runner.call(
                ModelContext(
                    messages=self.mode_router.build_messages(conversation.records)
                ),
                [],
                "routing",
                None,
                runtime_prompts=[self.mode_router.system_prompt],
            )
            checkpoints.extend(route_outcome.checkpoints)
            route_response = route_outcome.result
            if isinstance(route_response, Checkpoint):
                if route_response.reason in {
                    "empty_model_response",
                    "invalid_llm_response",
                }:
                    checkpoints.append(route_response)
                    checkpoints.append(
                        runtime_mode_fallback_checkpoint(
                            from_mode=None,
                            to_mode=RunMode.PLAIN.value,
                            reason=route_response.reason,
                        )
                    )
                    runtime_mode = self.runtime_modes[RunMode.PLAIN]
                else:
                    return ModeActivationOutcome(
                        runtime_mode=None,
                        mode_state=None,
                        context_state=context_state,
                        compressed=False,
                        checkpoints=tuple(checkpoints),
                        terminal_checkpoint=route_response,
                    )
            else:
                try:
                    decision = self.mode_router.accept_response(route_response)
                except InvalidLLMResponse as exc:
                    checkpoints.append(
                        runtime_mode_activation_failed_checkpoint(
                            mode=None,
                            stage="routing",
                            reason=exc.code,
                            error=str(exc),
                        )
                    )
                    checkpoints.append(
                        runtime_mode_fallback_checkpoint(
                            from_mode=None,
                            to_mode=RunMode.PLAIN.value,
                            reason=exc.code,
                        )
                    )
                    runtime_mode = self.runtime_modes[RunMode.PLAIN]
                else:
                    checkpoints.append(
                        runtime_mode_routed_checkpoint(
                            decision.mode.value,
                            decision.reason,
                        )
                    )
                    runtime_mode = self.runtime_modes[decision.mode]

        if runtime_mode is None:
            raise AssertionError("runtime mode was not configured")

        mode_state = runtime_mode.create_state()
        start_prompt = runtime_mode.start(mode_state)
        if start_prompt is None:
            return ModeActivationOutcome(
                runtime_mode,
                mode_state,
                context_state,
                False,
                tuple(checkpoints),
            )

        start_outcome = await self.model_turn_runner.prepare_and_call(
            conversation_records=conversation.records,
            context_state=context_state,
            runtime_instructions=[],
            tools=runtime_mode.runtime_tools(mode_state),
            level2_boundary_message_id=level2_boundary_message_id,
            stage="todo_initialization",
            round_index=None,
            system_prompt_override=start_prompt,
        )
        checkpoints.extend(start_outcome.checkpoints)
        start_response = start_outcome.result
        if isinstance(start_response, Checkpoint):
            return ModeActivationOutcome(
                runtime_mode=None,
                mode_state=None,
                context_state=start_outcome.context_state,
                compressed=start_outcome.compressed,
                checkpoints=tuple(checkpoints),
                terminal_checkpoint=start_response,
            )

        try:
            start_data = await runtime_mode.accept_start_response(
                mode_state,
                start_response,
            )
        except InvalidLLMResponse as exc:
            if self.mode_router is None:
                checkpoint = invalid_llm_response_checkpoint(
                    stage="todo_initialization",
                    round_index=None,
                    reason=exc.code,
                    error=str(exc),
                )
                return ModeActivationOutcome(
                    runtime_mode=None,
                    mode_state=None,
                    context_state=start_outcome.context_state,
                    compressed=start_outcome.compressed,
                    checkpoints=tuple(checkpoints),
                    terminal_checkpoint=checkpoint,
                )
            checkpoints.append(
                runtime_mode_activation_failed_checkpoint(
                    mode=RunMode.TODO.value,
                    stage="todo_initialization",
                    reason=exc.code,
                    error=str(exc),
                )
            )
            checkpoints.append(
                runtime_mode_fallback_checkpoint(
                    from_mode=RunMode.TODO.value,
                    to_mode=RunMode.PLAIN.value,
                    reason=exc.code,
                )
            )
            runtime_mode = self.runtime_modes[RunMode.PLAIN]
            mode_state = runtime_mode.create_state()
        else:
            if start_data is not None:
                checkpoints.append(todo_list_created_checkpoint(start_data))

        return ModeActivationOutcome(
            runtime_mode,
            mode_state,
            start_outcome.context_state,
            start_outcome.compressed,
            tuple(checkpoints),
        )
