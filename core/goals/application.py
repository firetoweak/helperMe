from __future__ import annotations

from dataclasses import dataclass

from core.goals.commands import (
    GoalCommandBufferRegistry,
    GoalCommandKind,
)
from core.goals.capabilities import (
    GoalPlanRevisionCapability,
    GoalTaskCapability,
)
from core.goals.goal import (
    Goal,
    OutcomeDecision,
    PlanRevision,
    TaskDraft,
    TaskOutcome,
)
from core.goals.verification import CompletionGate, CompletionReview
from core.goals.store import GoalStore
from core.session import SessionRunOutcome, SessionRuntime
from core.session.state import Session, SessionStatus
from core.tools_runtime.run_runtime import RunStatus
from core.tools_runtime.run_invocation import RunInvocation


@dataclass(frozen=True)
class GoalRunOutcome:
    goal_id: str
    task_id: str
    session_outcome: SessionRunOutcome
    applied_outcome: TaskOutcome | None
    completion_review: CompletionReview | None


@dataclass(frozen=True)
class GoalPlanRunOutcome:
    goal_id: str
    task_id: str
    session_outcome: SessionRunOutcome
    applied_revision: PlanRevision | None


class GoalApplicationService:
    def __init__(
        self,
        session_runtime: SessionRuntime,
        goal_store: GoalStore,
        command_buffers: GoalCommandBufferRegistry,
        completion_gate: CompletionGate | None = None,
    ) -> None:
        self._sessions = session_runtime
        self._goals = goal_store
        self._command_buffers = command_buffers
        self._completion_gate = completion_gate or CompletionGate()

    def create_goal(
        self,
        session_id: str,
        goal_id: str,
        objective: str,
        tasks: list[TaskDraft],
    ) -> Goal:
        self._sessions.get_session(session_id)
        goal = Goal(goal_id, objective, tasks)
        self._goals.add(session_id, goal)
        return goal

    def execute_next_task(
        self,
        session_id: str,
        goal_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int = 20,
    ) -> GoalRunOutcome:
        goal = self._goal_for_session(session_id, goal_id)
        session = self._validate_session_run(
            session_id,
            run_id,
            user_message,
        )

        task = goal.next_task()
        if task is None:
            raise ValueError("goal has no executable task")

        goal.start_task_run(task.id, run_id)
        buffer = self._command_buffers.open(
            goal.id,
            task.id,
            run_id,
            GoalCommandKind.TASK_OUTCOME,
        )
        invocation = RunInvocation(
            capabilities=(
                GoalTaskCapability(
                    goal_id=goal.id,
                    objective=goal.objective,
                    task=goal.task(task.id),
                    command_buffer=buffer,
                    completion_gate=self._completion_gate,
                ),
            )
        )

        try:
            session_outcome = self._execute_session_run(
                session_id,
                run_id,
                user_message,
                max_rounds,
                session.status,
                invocation,
            )
        except Exception:
            goal.abort_task_run(run_id)
            self._command_buffers.close(goal.id, run_id)
            raise

        goal.finish_task_run(run_id)
        buffer = self._command_buffers.close(goal.id, run_id)
        applied_outcome = None
        completion_review = None
        if session_outcome.result.status is RunStatus.COMPLETED:
            candidate = buffer.task_outcome
            if candidate is not None:
                if candidate.decision is OutcomeDecision.COMPLETED:
                    completion_review = self._completion_gate.review(
                        task.verification,
                        task.acceptance_criteria,
                        session_outcome.result.evidence,
                    )
                if completion_review is None or completion_review.accepted:
                    goal.record_outcome(candidate)
                    applied_outcome = candidate

        return GoalRunOutcome(
            goal_id=goal.id,
            task_id=task.id,
            session_outcome=session_outcome,
            applied_outcome=applied_outcome,
            completion_review=completion_review,
        )

    def execute_plan_revision(
        self,
        session_id: str,
        goal_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int = 20,
    ) -> GoalPlanRunOutcome:
        goal = self._goal_for_session(session_id, goal_id)
        session = self._validate_session_run(
            session_id,
            run_id,
            user_message,
        )
        task_id = goal.replan_task_id

        goal.start_plan_revision_run(run_id)
        buffer = self._command_buffers.open(
            goal.id,
            task_id,
            run_id,
            GoalCommandKind.PLAN_REVISION,
        )
        invocation = RunInvocation(
            capabilities=(
                GoalPlanRevisionCapability(
                    goal=goal,
                    task_id=task_id,
                    command_buffer=buffer,
                ),
            )
        )
        try:
            session_outcome = self._execute_session_run(
                session_id,
                run_id,
                user_message,
                max_rounds,
                session.status,
                invocation,
            )
        except Exception:
            goal.abort_plan_revision_run(run_id)
            self._command_buffers.close(goal.id, run_id)
            raise

        goal.finish_plan_revision_run(run_id)
        buffer = self._command_buffers.close(goal.id, run_id)
        applied_revision = None
        if session_outcome.result.status is RunStatus.COMPLETED:
            applied_revision = buffer.plan_revision
            if applied_revision is not None:
                goal.apply_plan_revision(applied_revision)

        return GoalPlanRunOutcome(
            goal_id=goal.id,
            task_id=task_id,
            session_outcome=session_outcome,
            applied_revision=applied_revision,
        )

    def apply_plan_revision(
        self,
        goal_id: str,
        revision: PlanRevision,
    ) -> None:
        self._goals.get(goal_id).apply_plan_revision(revision)

    def _goal_for_session(self, session_id: str, goal_id: str) -> Goal:
        goal = self._goals.get(goal_id)
        bound_session_id = self._goals.session_id_for(goal_id)
        if bound_session_id != session_id:
            raise ValueError(
                f"goal {goal_id} belongs to session {bound_session_id}, "
                f"not {session_id}"
            )
        return goal

    def _validate_session_run(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
    ) -> Session:
        self._sessions.validate_run_input(run_id, user_message)
        session = self._sessions.get_session(session_id)
        if session.status is SessionStatus.RUNNING:
            raise ValueError(f"Session 已在执行: {session_id}")
        if any(record.run_id == run_id for record in session.run_records):
            raise ValueError(f"重复 run_id: {run_id}")
        return session

    def _execute_session_run(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int,
        session_status: SessionStatus,
        invocation: RunInvocation,
    ) -> SessionRunOutcome:
        if session_status is SessionStatus.INTERRUPTED:
            return self._sessions.resume(
                session_id,
                run_id,
                user_message,
                max_rounds,
                invocation=invocation,
            )
        return self._sessions.start(
            session_id,
            run_id,
            user_message,
            max_rounds,
            invocation=invocation,
        )
