from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from core.run_host import RunHost
from core.runtime_modes import PlainMode
from core.session import SessionRunOutcome
from core.tools_runtime.run_invocation import RunInvocation
from core.tools_runtime.run_runtime import RunStatus
from plugins.goal.capabilities import (
    ContractCompilationCapability,
    GoalExecutorCapability,
    GoalJudgeCapability,
)
from plugins.goal.submissions import (
    ContractCompilationBuffer,
    JudgmentBuffer,
)
from plugins.goal.goal import (
    CompletionContract,
    Goal,
    GoalJudgment,
    GoalStatus,
)
from plugins.goal.store import GoalStore


ContinuationFactory = Callable[[Goal], str]
IdFactory = Callable[[], str]


@dataclass(frozen=True)
class GoalTurnOutcome:
    executor_outcome: SessionRunOutcome | None
    judge_outcome: SessionRunOutcome | None
    judgment: GoalJudgment | None


@dataclass(frozen=True)
class GoalLoopOutcome:
    goal: Goal | None
    contract_outcome: SessionRunOutcome | None
    turns: tuple[GoalTurnOutcome, ...]

    @property
    def final_session_outcome(self) -> SessionRunOutcome:
        if self.turns:
            latest = self.turns[-1]
            return latest.judge_outcome or latest.executor_outcome
        if self.contract_outcome is not None:
            return self.contract_outcome
        raise ValueError("goal loop produced no run outcome")


class GoalApplicationService:
    def __init__(
        self,
        run_host: RunHost,
        goal_store: GoalStore,
        *,
        default_max_turns: int,
        id_factory: IdFactory | None = None,
        continuation_factory: ContinuationFactory | None = None,
    ) -> None:
        if default_max_turns < 1:
            raise ValueError("default_max_turns must be positive")
        self._run_host = run_host
        self._goals = goal_store
        self._default_max_turns = default_max_turns
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._continuation_factory = (
            continuation_factory or self._default_continuation
        )
        self._active_run_sessions: dict[str, str] = {}

    def start_goal(
        self,
        session_id: str,
        goal_id: str,
        first_executor_run_id: str,
        objective: str,
        max_turns: int | None = None,
        max_rounds: int | None = None,
    ) -> GoalLoopOutcome:
        self._run_host.require_session(session_id)
        self._run_host.validate_run(
            session_id,
            first_executor_run_id,
            objective,
        )
        active = self._goals.active_for_session(session_id)
        if active is not None:
            raise ValueError(
                f"session {session_id} already has active goal: {active.id}"
            )

        contract_buffer = ContractCompilationBuffer()
        contract_outcome = self._execute_isolated(
            owner_session_id=session_id,
            purpose="contract",
            user_message=f"为以下 Goal 编译 Completion Contract：\n{objective}",
            max_rounds=max_rounds,
            invocation=RunInvocation(
                capabilities=(
                    ContractCompilationCapability(objective, contract_buffer),
                ),
                runtime_mode=PlainMode(),
            ),
        )
        if contract_outcome.result.status is not RunStatus.COMPLETED:
            return GoalLoopOutcome(None, contract_outcome, ())
        if contract_buffer.contract is None:
            raise RuntimeError("completed contract run has no contract")

        resolved_max_turns = (
            self._default_max_turns if max_turns is None else max_turns
        )
        goal = Goal(
            goal_id,
            objective,
            CompletionContract.initial(contract_buffer.contract),
            resolved_max_turns,
        )
        self._goals.add(session_id, goal)
        turns = self._drive(
            session_id=session_id,
            goal=goal,
            first_executor_run_id=first_executor_run_id,
            first_message=objective,
            max_rounds=max_rounds,
        )
        return GoalLoopOutcome(goal, contract_outcome, tuple(turns))

    def continue_goal(
        self,
        session_id: str,
        goal_id: str,
        executor_run_id: str,
        user_message: str,
        max_rounds: int | None = None,
    ) -> GoalLoopOutcome:
        goal = self._goal_for_session(session_id, goal_id)
        if goal.status is GoalStatus.PAUSED:
            goal.resume()
        elif goal.status not in {GoalStatus.ACTIVE, GoalStatus.JUDGING}:
            raise ValueError(f"goal cannot continue from {goal.status.value}")

        turns = self._drive(
            session_id=session_id,
            goal=goal,
            first_executor_run_id=executor_run_id,
            first_message=user_message,
            max_rounds=max_rounds,
        )
        return GoalLoopOutcome(goal, None, tuple(turns))

    def active_goal(self, session_id: str) -> Goal | None:
        self._run_host.require_session(session_id)
        return self._goals.active_for_session(session_id)

    def request_pause(self, session_id: str) -> bool:
        active_run_session = self._active_run_sessions.get(session_id)
        if active_run_session is None:
            return False
        self._run_host.request_interrupt(
            active_run_session,
            "goal_pause_requested",
        )
        return True

    def _drive(
        self,
        *,
        session_id: str,
        goal: Goal,
        first_executor_run_id: str,
        first_message: str,
        max_rounds: int | None,
    ) -> list[GoalTurnOutcome]:
        outcomes: list[GoalTurnOutcome] = []
        next_run_id = first_executor_run_id
        next_message = first_message

        while goal.status in {GoalStatus.ACTIVE, GoalStatus.JUDGING}:
            if goal.status is GoalStatus.JUDGING:
                outcomes.append(
                    self._judge_current_turn(session_id, goal, None, max_rounds)
                )
                continue

            self._run_host.validate_run(
                session_id,
                next_run_id,
                next_message,
            )
            turn = goal.start_turn(next_run_id)
            try:
                executor_outcome = self._execute(
                    owner_session_id=session_id,
                    actual_session_id=session_id,
                    run_id=next_run_id,
                    user_message=next_message,
                    max_rounds=max_rounds,
                    invocation=RunInvocation(
                        capabilities=(
                            GoalExecutorCapability(
                                goal_id=goal.id,
                                objective=goal.objective,
                                turn_index=turn.index,
                                contract=goal.contract,
                                judge_feedback=goal.latest_feedback,
                            ),
                        ),
                    ),
                )
            except Exception:
                goal.interrupt_turn(
                    next_run_id,
                    "executor_runtime_exception",
                )
                raise
            if executor_outcome.result.status is not RunStatus.COMPLETED:
                goal.interrupt_turn(
                    next_run_id,
                    executor_outcome.result.final_reason
                    or executor_outcome.result.status.value,
                )
                outcomes.append(GoalTurnOutcome(executor_outcome, None, None))
                break

            judge_run_id = f"judge-{self._id_factory()}"
            goal.begin_judgment(
                next_run_id,
                judge_run_id,
                executor_outcome.result.answer,
            )
            outcomes.append(
                self._judge_current_turn(
                    session_id,
                    goal,
                    executor_outcome,
                    max_rounds,
                    judge_run_id=judge_run_id,
                )
            )
            next_run_id = f"goal-turn-{self._id_factory()}"
            next_message = self._continuation_factory(goal)

        return outcomes

    def _judge_current_turn(
        self,
        session_id: str,
        goal: Goal,
        executor_outcome: SessionRunOutcome | None,
        max_rounds: int | None,
        *,
        judge_run_id: str | None = None,
    ) -> GoalTurnOutcome:
        turn = goal.turns[-1]
        resolved_judge_run_id = judge_run_id or turn.judge_run_id
        if resolved_judge_run_id is None:
            raise RuntimeError("judging turn has no judge_run_id")
        if turn.executor_answer is None:
            raise RuntimeError("judging turn has no executor answer")

        buffer = JudgmentBuffer()
        judge_outcome = self._execute_isolated(
            owner_session_id=session_id,
            purpose="judge",
            user_message="独立验证当前 Goal 是否完成。",
            max_rounds=max_rounds,
            invocation=RunInvocation(
                capabilities=(
                    GoalJudgeCapability(
                        goal_id=goal.id,
                        objective=goal.objective,
                        turn_index=turn.index,
                        contract=goal.contract,
                        executor_answer=turn.executor_answer,
                        buffer=buffer,
                    ),
                ),
                runtime_mode=PlainMode(),
            ),
            run_id=resolved_judge_run_id,
        )
        if judge_outcome.result.status is not RunStatus.COMPLETED:
            goal.pause_judgment(
                judge_outcome.result.final_reason
                or judge_outcome.result.status.value
            )
            return GoalTurnOutcome(executor_outcome, judge_outcome, None)
        if buffer.submission is None:
            raise RuntimeError("completed judge run has no judgment")

        submission = buffer.submission
        if submission.contract_revision is not None:
            goal.revise_contract(
                submission.contract_revision,
                submission.revision_reason,
            )
        judgment = GoalJudgment(
            turn_index=turn.index,
            judge_run_id=resolved_judge_run_id,
            decision=submission.decision,
            reason=submission.reason,
            evidence=submission.evidence,
        )
        goal.record_judgment(judgment)
        return GoalTurnOutcome(executor_outcome, judge_outcome, judgment)

    def _execute_isolated(
        self,
        *,
        owner_session_id: str,
        purpose: str,
        user_message: str,
        max_rounds: int | None,
        invocation: RunInvocation,
        run_id: str | None = None,
    ) -> SessionRunOutcome:
        suffix = self._id_factory()
        isolated_session_id = f"goal-{purpose}-{suffix}"
        isolated_run_id = run_id or f"{purpose}-run-{suffix}"
        self._run_host.create_session(isolated_session_id)
        try:
            return self._execute(
                owner_session_id=owner_session_id,
                actual_session_id=isolated_session_id,
                run_id=isolated_run_id,
                user_message=user_message,
                max_rounds=max_rounds,
                invocation=invocation,
            )
        finally:
            self._run_host.delete_session(isolated_session_id)

    def _execute(
        self,
        *,
        owner_session_id: str,
        actual_session_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int | None,
        invocation: RunInvocation,
    ) -> SessionRunOutcome:
        self._active_run_sessions[owner_session_id] = actual_session_id
        try:
            return self._run_host.execute(
                actual_session_id,
                run_id,
                user_message,
                max_rounds,
                invocation,
            )
        finally:
            del self._active_run_sessions[owner_session_id]

    def _goal_for_session(self, session_id: str, goal_id: str) -> Goal:
        goal = self._goals.get(goal_id)
        bound_session_id = self._goals.session_id_for(goal_id)
        if bound_session_id != session_id:
            raise ValueError(
                f"goal {goal_id} belongs to session {bound_session_id}, "
                f"not {session_id}"
            )
        return goal

    @staticmethod
    def _default_continuation(goal: Goal) -> str:
        return "\n".join(
            [
                "Continue working toward the active Goal.",
                f"Goal: {goal.objective}",
                f"Judge feedback: {goal.latest_feedback}",
            ]
        )
