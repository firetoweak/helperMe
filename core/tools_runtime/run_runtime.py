from __future__ import annotations

import asyncio  # 兼容既有重试测试的 patch 边界；重试实现位于 model_turn。
import json
from core.tools_runtime.tools_checkpoint import (
    Checkpoint,
    budget_stop_checkpoint,
    format_checkpoint,
    message_chain_invalid_checkpoint,
    run_completed_checkpoint,
    run_interrupted_checkpoint,
    run_started_checkpoint,
    tool_batch_completed_checkpoint,
    verification_required_checkpoint,
)
from core.messages import Conversation
from core.model_call.service import ModelCallService
from core.tools_runtime.stop_guard import evaluate_stop_safety
from core.tools_runtime.tools_executor import ToolsExecutor
from core.tools_runtime.tools_protocol import validate_tool_message_chain
from core.tools_runtime.tools_state import ToolsState
from core.runtime_modes import RunMode, RuntimeMode, RuntimeModeRouter
from core.context import ContextPreparationService, ContextState
from core.runtime_artifacts import ToolResultExternalizer
from core.tools_runtime.run_progress import NullRunProgressSink, RunProgressSink
from core.tools_runtime.run_evidence import RunEvidence, RunEvidenceRecorder
from core.tools_runtime.run_invocation import RunInvocation
from core.tools_runtime.model_turn import ModelTurnRunner
from core.tools_runtime.mode_activation import ModeActivator
from core.tools_runtime.run_types import RunControl, RunResult, RunStatus
from core.tools_runtime.tool_batch import ConcurrentToolBatchExecutor
from core.tools_runtime.tool_environment import RunToolEnvironment


class RunRuntime:
    """最小 tool-calling 运行内核。"""

    def __init__(
        self,
        model_calls: ModelCallService,
        model: str,
        runtime_mode: RuntimeMode | None = None,
        context_preparation: ContextPreparationService | None = None,
        tools_executor: ToolsExecutor | None = None,
        tool_result_externalizer: ToolResultExternalizer | None = None,
        progress_sink: RunProgressSink | None = None,
        *,
        mode_router: RuntimeModeRouter | None = None,
        runtime_modes: dict[RunMode, RuntimeMode] | None = None,
    ):
        if runtime_mode is None:
            if mode_router is None or runtime_modes is None:
                raise ValueError(
                    "mode_router and runtime_modes are required without runtime_mode"
                )
        elif mode_router is not None or runtime_modes is not None:
            raise ValueError(
                "runtime_mode cannot be combined with mode_router/runtime_modes"
            )
        self.model_calls = model_calls
        self.model = model
        self.runtime_mode = runtime_mode
        self.mode_router = mode_router
        self.runtime_modes = runtime_modes
        self.context_preparation = context_preparation
        self.tools_executor = tools_executor
        self.tool_result_externalizer = tool_result_externalizer
        self.progress_sink = progress_sink or NullRunProgressSink()

    @staticmethod
    def _finish(
        *,
        status: RunStatus,
        answer: str,
        checkpoint: Checkpoint,
        checkpoints: list[Checkpoint],
        context_state: ContextState,
        evidence: RunEvidence,
    ) -> RunResult:
        checkpoints.append(checkpoint)
        return RunResult(
            status=status,
            answer=answer,
            checkpoints=checkpoints,
            context_state=context_state,
            evidence=evidence,
        )

    async def run(
        self,
        conversation: Conversation,
        user_message: str,
        max_rounds: int = 50,
        control: RunControl | None = None,
        context_state: ContextState | None = None,
        invocation: RunInvocation | None = None,
    ) -> RunResult:
        checkpoints: list[Checkpoint] = []
        tools_state = ToolsState()
        evidence_recorder = RunEvidenceRecorder()
        run_control = control or RunControl()
        current_context_state = context_state or ContextState()
        current_invocation = invocation or RunInvocation()
        model_turn_runner = ModelTurnRunner(
            self.model_calls,
            self.model,
            self.context_preparation,
        )
        tool_environment = RunToolEnvironment(
            self.tools_executor,
            current_invocation,
        )
        tool_batch_executor = ConcurrentToolBatchExecutor(
            self.tool_result_externalizer,
        )
        for root in tool_environment.evidence_roots():
            evidence_recorder.record_workspace_baseline(
                root,
                await tool_environment.base_executor.execute(
                    "get_changes",
                    json.dumps({"root": root}),
                ),
            )
        level2_performed = False
        level2_boundary_message_id = (
            conversation.records[-1].message_id
            if conversation.records
            else None
        )
        system_prompt = (
            conversation.records[0].payload.get("content")
            if conversation.records
            and conversation.records[0].payload.get("role") == "system"
            else None
        )
        checkpoints.append(run_started_checkpoint(max_rounds, system_prompt))
        conversation.add_user(user_message)

        activation = await ModeActivator(
            model_turn_runner=model_turn_runner,
            default_mode=self.runtime_mode,
            mode_router=self.mode_router,
            runtime_modes=self.runtime_modes,
        ).activate(
            conversation=conversation,
            requested_mode=current_invocation.runtime_mode,
            context_state=current_context_state,
            level2_boundary_message_id=level2_boundary_message_id,
        )
        checkpoints.extend(activation.checkpoints)
        current_context_state = activation.context_state
        level2_performed = level2_performed or activation.compressed
        if activation.terminal_checkpoint is not None:
            checkpoint = activation.terminal_checkpoint
            status = (
                RunStatus.BLOCKED
                if checkpoint.reason in {
                    "context_budget_exceeded",
                    "context_length_exceeded",
                }
                else RunStatus.FAILED
            )
            return self._finish(
                status=status,
                answer=format_checkpoint(checkpoint),
                checkpoint=checkpoint,
                checkpoints=checkpoints,
                context_state=current_context_state,
                evidence=evidence_recorder.snapshot(),
            )
        runtime_mode = activation.runtime_mode
        mode_state = activation.mode_state
        if runtime_mode is None:
            raise AssertionError("mode activation completed without a runtime mode")
        for round_index in range(1, max_rounds + 1):
            tool_snapshot = tool_environment.snapshot(runtime_mode, mode_state)
            tools = tool_snapshot.model_tools
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
                    evidence=evidence_recorder.snapshot(),
                )
            turn_outcome = await model_turn_runner.prepare_and_call(
                conversation_records=conversation.records,
                context_state=current_context_state,
                runtime_instructions=tool_snapshot.runtime_prompts,
                tools=tools,
                level2_boundary_message_id=level2_boundary_message_id,
                stage="agent_round",
                round_index=round_index,
                preparation_failure_stage="context_summary",
            )
            checkpoints.extend(turn_outcome.checkpoints)
            current_context_state = turn_outcome.context_state
            level2_performed = level2_performed or turn_outcome.compressed
            llm_outcome = turn_outcome.result
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
                    evidence=evidence_recorder.snapshot(),
                )
            response = llm_outcome

            conversation.add_assistant(response)
            if response.calls:
                if response.content.strip():
                    self.progress_sink.emit(response.content)
            else:
                final_feedback = runtime_mode.check_final_candidate(
                    mode_state
                )
                if final_feedback is not None:
                    conversation.add_user(final_feedback)
                    continue
                for capability in current_invocation.capabilities:
                    final_feedback = capability.check_final_candidate(
                        evidence_recorder.snapshot()
                    )
                    if final_feedback is not None:
                        conversation.add_user(final_feedback)
                        break
                if final_feedback is not None:
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
                        evidence=evidence_recorder.snapshot(),
                    )

                if not stop_safety.business_safe:
                    checkpoint = verification_required_checkpoint()
                    checkpoints.append(checkpoint)
                    conversation.add_user(checkpoint.message)
                    continue

                answer = response.content
                if level2_performed:
                    answer = "本轮已执行上下文压缩。\n\n" + answer
                runtime_mode.on_run_completed(mode_state)
                completion_data = runtime_mode.checkpoint_data(mode_state) or {}
                for capability in current_invocation.capabilities:
                    capability_data = capability.checkpoint_data() or {}
                    duplicated_keys = completion_data.keys() & capability_data.keys()
                    if duplicated_keys:
                        raise ValueError(
                            "capability checkpoint data conflicts with existing "
                            f"data: {sorted(duplicated_keys)}"
                        )
                    completion_data.update(capability_data)
                checkpoint = run_completed_checkpoint(
                    answer=answer,
                    extra_data=completion_data or None,
                )
                return self._finish(
                    status=RunStatus.COMPLETED,
                    answer=answer,
                    checkpoint=checkpoint,
                    checkpoints=checkpoints,
                    context_state=current_context_state,
                    evidence=evidence_recorder.snapshot(),
                )

            calls = response.calls
            batch = await tool_batch_executor.execute(
                calls=calls,
                tools_executor=tool_snapshot.executor,
                runtime_mode=runtime_mode,
                mode_state=mode_state,
                tools_state=tools_state,
                evidence_recorder=evidence_recorder,
            )
            conversation.add_tools_result(batch.messages)
            batch_feedback = runtime_mode.after_tool_batch(
                mode_state,
                batch.steps,
            )
            if batch_feedback is not None:
                conversation.add_user(batch_feedback)
            checkpoints.append(
                tool_batch_completed_checkpoint(
                    round_index,
                    tools_state,
                    len(calls),
                    runtime_mode.checkpoint_data(mode_state),
                    result_chars_before=batch.result_chars_before,
                    result_chars_after=batch.result_chars_after,
                    externalized_count=batch.externalized_count,
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
                        evidence=evidence_recorder.snapshot(),
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
                    evidence=evidence_recorder.snapshot(),
                )

        checkpoint = budget_stop_checkpoint(max_rounds, tools_state)
        return self._finish(
            status=RunStatus.BLOCKED,
            answer=format_checkpoint(checkpoint),
            checkpoint=checkpoint,
            checkpoints=checkpoints,
            context_state=current_context_state,
            evidence=evidence_recorder.snapshot(),
        )
