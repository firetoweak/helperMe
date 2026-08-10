from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.goals.goal import PlanRevision, TaskOutcome


class GoalCommandKind(str, Enum):
    TASK_OUTCOME = "task_outcome"
    PLAN_REVISION = "plan_revision"


@dataclass
class GoalCommandBuffer:
    goal_id: str
    task_id: str
    run_id: str
    expected_kind: GoalCommandKind
    task_outcome: TaskOutcome | None = None
    plan_revision: PlanRevision | None = None

    def submit_task_outcome(self, outcome: TaskOutcome) -> None:
        if self.expected_kind is not GoalCommandKind.TASK_OUTCOME:
            raise ValueError("this run does not accept a task outcome")
        if outcome.task_id != self.task_id:
            raise ValueError(
                f"outcome must target task {self.task_id}: {outcome.task_id}"
            )
        if outcome.run_id != self.run_id:
            raise ValueError(
                f"outcome must target run {self.run_id}: {outcome.run_id}"
            )
        self.task_outcome = outcome

    def submit_plan_revision(self, revision: PlanRevision) -> None:
        if self.expected_kind is not GoalCommandKind.PLAN_REVISION:
            raise ValueError("this run does not accept a plan revision")
        if self.plan_revision is not None:
            raise ValueError("plan revision already submitted")
        if revision.task_id != self.task_id:
            raise ValueError(
                f"revision must target task {self.task_id}: {revision.task_id}"
            )
        self.plan_revision = revision


class GoalCommandBufferRegistry:
    def __init__(self) -> None:
        self._buffers: dict[tuple[str, str], GoalCommandBuffer] = {}

    def open(
        self,
        goal_id: str,
        task_id: str,
        run_id: str,
        expected_kind: GoalCommandKind,
    ) -> GoalCommandBuffer:
        key = (goal_id, run_id)
        if key in self._buffers:
            raise ValueError(f"command buffer already exists: {key}")
        buffer = GoalCommandBuffer(
            goal_id=goal_id,
            task_id=task_id,
            run_id=run_id,
            expected_kind=expected_kind,
        )
        self._buffers[key] = buffer
        return buffer

    def get(self, goal_id: str, run_id: str) -> GoalCommandBuffer:
        return self._buffers[(goal_id, run_id)]

    def close(self, goal_id: str, run_id: str) -> GoalCommandBuffer:
        return self._buffers.pop((goal_id, run_id))
