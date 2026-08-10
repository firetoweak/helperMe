from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.goals.verification import TaskVerification


class GoalStatus(str, Enum):
    ACTIVE = "active"
    REPLAN_REQUIRED = "replan_required"
    COMPLETED = "completed"


class TaskStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class OutcomeDecision(str, Enum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    REPLAN = "replan"


@dataclass(frozen=True)
class TaskDraft:
    id: str
    description: str
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: str | None = None
    verification: TaskVerification | None = None


@dataclass(frozen=True)
class Task:
    id: str
    description: str
    depends_on: tuple[str, ...]
    acceptance_criteria: str | None
    verification: TaskVerification | None = None
    status: TaskStatus = TaskStatus.PENDING


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    run_id: str
    decision: OutcomeDecision
    summary: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskRunLink:
    goal_id: str
    task_id: str
    run_id: str


@dataclass(frozen=True)
class DependencyChange:
    task_id: str
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class PlanRevision:
    task_id: str
    reason: str
    replacement_tasks: tuple[TaskDraft, ...]
    dependency_changes: tuple[DependencyChange, ...] = ()


class Goal:
    def __init__(
        self,
        goal_id: str,
        objective: str,
        tasks: list[TaskDraft],
    ) -> None:
        if not goal_id.strip():
            raise ValueError("goal_id cannot be empty")
        if not objective.strip():
            raise ValueError("objective cannot be empty")
        if not tasks:
            raise ValueError("goal must contain at least one task")

        initial_tasks = [
            Task(
                id=draft.id,
                description=draft.description,
                depends_on=draft.depends_on,
                acceptance_criteria=draft.acceptance_criteria,
                verification=draft.verification,
            )
            for draft in tasks
        ]
        self._validate_plan(initial_tasks)

        self.id = goal_id
        self.objective = objective
        self._tasks = initial_tasks
        self._status = GoalStatus.ACTIVE
        self._outcomes: list[TaskOutcome] = []
        self._run_links: list[TaskRunLink] = []
        self._finished_run_ids: set[str] = set()
        self._aborted_run_ids: set[str] = set()
        self._open_run_id: str | None = None
        self._replan_task_id: str | None = None

    @property
    def status(self) -> GoalStatus:
        return self._status

    @property
    def tasks(self) -> tuple[Task, ...]:
        return tuple(self._tasks)

    @property
    def outcomes(self) -> tuple[TaskOutcome, ...]:
        return tuple(self._outcomes)

    @property
    def run_links(self) -> tuple[TaskRunLink, ...]:
        return tuple(self._run_links)

    @property
    def replan_task_id(self) -> str:
        if self._status is not GoalStatus.REPLAN_REQUIRED:
            raise ValueError("goal does not require replanning")
        return self._replan_task_id

    def task(self, task_id: str) -> Task:
        return next(task for task in self._tasks if task.id == task_id)

    def next_task(self) -> Task | None:
        if self._status is not GoalStatus.ACTIVE:
            return None

        active = next(
            (
                task
                for task in self._tasks
                if task.status is TaskStatus.ACTIVE
            ),
            None,
        )
        if active is not None:
            return active

        statuses = {task.id: task.status for task in self._tasks}
        return next(
            (
                task
                for task in self._tasks
                if task.status is TaskStatus.PENDING
                and all(
                    statuses[dependency] is TaskStatus.COMPLETED
                    for dependency in task.depends_on
                )
            ),
            None,
        )

    def start_task_run(self, task_id: str, run_id: str) -> None:
        if not run_id.strip():
            raise ValueError("run_id cannot be empty")
        if self._status is not GoalStatus.ACTIVE:
            raise ValueError("only an active goal can start a task run")
        if self._open_run_id is not None:
            raise ValueError(f"run still awaits an outcome: {self._open_run_id}")
        if any(link.run_id == run_id for link in self._run_links):
            raise ValueError(f"duplicate run_id: {run_id}")

        next_task = self.next_task()
        if next_task is None:
            raise ValueError("goal has no executable task")
        if next_task.id != task_id:
            raise ValueError(
                f"task {task_id} is not the next executable task: "
                f"{next_task.id}"
            )

        if next_task.status is TaskStatus.PENDING:
            self._replace_task_status(task_id, TaskStatus.ACTIVE)
        self._run_links.append(TaskRunLink(self.id, task_id, run_id))
        self._open_run_id = run_id

    def finish_task_run(self, run_id: str) -> None:
        self._finish_run(run_id)

    def abort_task_run(self, run_id: str) -> None:
        self._abort_run(run_id)

    def start_plan_revision_run(self, run_id: str) -> None:
        if not run_id.strip():
            raise ValueError("run_id cannot be empty")
        task_id = self.replan_task_id
        if self._open_run_id is not None:
            raise ValueError(f"run still awaits completion: {self._open_run_id}")
        if any(link.run_id == run_id for link in self._run_links):
            raise ValueError(f"duplicate run_id: {run_id}")
        self._run_links.append(TaskRunLink(self.id, task_id, run_id))
        self._open_run_id = run_id

    def finish_plan_revision_run(self, run_id: str) -> None:
        self._finish_run(run_id)

    def abort_plan_revision_run(self, run_id: str) -> None:
        self._abort_run(run_id)

    def _finish_run(self, run_id: str) -> None:
        if run_id != self._open_run_id:
            raise ValueError(
                f"run does not match the open task run: {run_id}"
            )
        self._finished_run_ids.add(run_id)
        self._open_run_id = None

    def _abort_run(self, run_id: str) -> None:
        if run_id != self._open_run_id:
            raise ValueError(
                f"run does not match the open task run: {run_id}"
            )
        self._aborted_run_ids.add(run_id)
        self._open_run_id = None

    def record_outcome(self, outcome: TaskOutcome) -> None:
        if self._status is not GoalStatus.ACTIVE:
            raise ValueError("only an active goal can record an outcome")
        if outcome.run_id not in self._finished_run_ids:
            raise ValueError(f"outcome run is not finished: {outcome.run_id}")
        if any(item.run_id == outcome.run_id for item in self._outcomes):
            raise ValueError(f"run already has an outcome: {outcome.run_id}")
        linked_task_id = next(
            link.task_id
            for link in self._run_links
            if link.run_id == outcome.run_id
        )
        if linked_task_id != outcome.task_id:
            raise ValueError(
                f"run {outcome.run_id} is not bound to task {outcome.task_id}"
            )
        if self.task(outcome.task_id).status is not TaskStatus.ACTIVE:
            raise ValueError("outcome task must be active")

        self._outcomes.append(outcome)

        if outcome.decision is OutcomeDecision.CONTINUE:
            return
        if outcome.decision is OutcomeDecision.COMPLETED:
            self._replace_task_status(outcome.task_id, TaskStatus.COMPLETED)
            if all(
                task.status in {TaskStatus.COMPLETED, TaskStatus.SUPERSEDED}
                for task in self._tasks
            ):
                self._status = GoalStatus.COMPLETED
            return

        self._status = GoalStatus.REPLAN_REQUIRED
        self._replan_task_id = outcome.task_id

    def apply_plan_revision(self, revision: PlanRevision) -> None:
        candidate = self._build_plan_revision(revision)
        self._tasks = candidate
        self._status = GoalStatus.ACTIVE
        self._replan_task_id = None

    def validate_plan_revision(self, revision: PlanRevision) -> None:
        self._build_plan_revision(revision)

    def _build_plan_revision(self, revision: PlanRevision) -> list[Task]:
        if self._status is not GoalStatus.REPLAN_REQUIRED:
            raise ValueError("goal does not require replanning")
        if revision.task_id != self._replan_task_id:
            raise ValueError(
                f"revision must replace task {self._replan_task_id}"
            )
        if not revision.reason.strip():
            raise ValueError("revision reason cannot be empty")
        if not revision.replacement_tasks:
            raise ValueError("revision must contain replacement tasks")

        replaced_index = self._task_index(revision.task_id)
        candidate = list(self._tasks)
        candidate[replaced_index] = self._with_status(
            candidate[replaced_index],
            TaskStatus.SUPERSEDED,
        )
        replacements = [
            Task(
                id=draft.id,
                description=draft.description,
                depends_on=draft.depends_on,
                acceptance_criteria=draft.acceptance_criteria,
                verification=draft.verification,
            )
            for draft in revision.replacement_tasks
        ]
        candidate[replaced_index + 1:replaced_index + 1] = replacements

        changed_ids: set[str] = set()
        for change in revision.dependency_changes:
            if change.task_id in changed_ids:
                raise ValueError(
                    f"duplicate dependency change: {change.task_id}"
                )
            changed_ids.add(change.task_id)
            index = self._task_index_in(candidate, change.task_id)
            task = candidate[index]
            if task.status in {TaskStatus.COMPLETED, TaskStatus.SUPERSEDED}:
                raise ValueError(
                    f"cannot change dependencies of {task.status.value} task: "
                    f"{task.id}"
                )
            candidate[index] = Task(
                id=task.id,
                description=task.description,
                depends_on=change.depends_on,
                acceptance_criteria=task.acceptance_criteria,
                verification=task.verification,
                status=task.status,
            )

        self._validate_plan(candidate)
        return candidate

    def _replace_task_status(
        self,
        task_id: str,
        status: TaskStatus,
    ) -> None:
        index = self._task_index(task_id)
        self._tasks[index] = self._with_status(self._tasks[index], status)

    def _task_index(self, task_id: str) -> int:
        return self._task_index_in(self._tasks, task_id)

    @staticmethod
    def _task_index_in(tasks: list[Task], task_id: str) -> int:
        return next(index for index, task in enumerate(tasks) if task.id == task_id)

    @staticmethod
    def _with_status(task: Task, status: TaskStatus) -> Task:
        return Task(
            id=task.id,
            description=task.description,
            depends_on=task.depends_on,
            acceptance_criteria=task.acceptance_criteria,
            verification=task.verification,
            status=status,
        )

    @staticmethod
    def _validate_plan(tasks: list[Task]) -> None:
        ids = [task.id for task in tasks]
        if any(not task_id.strip() for task_id in ids):
            raise ValueError("task id cannot be empty")
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        if any(not task.description.strip() for task in tasks):
            raise ValueError("task description cannot be empty")

        task_by_id = {task.id: task for task in tasks}
        for task in tasks:
            if len(task.depends_on) != len(set(task.depends_on)):
                raise ValueError(f"duplicate dependency on task: {task.id}")
            for dependency in task.depends_on:
                if dependency not in task_by_id:
                    raise ValueError(
                        f"unknown dependency {dependency} on task {task.id}"
                    )
                if task_by_id[dependency].status is TaskStatus.SUPERSEDED:
                    raise ValueError(
                        f"task {task.id} depends on superseded task {dependency}"
                    )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task dependencies must not contain a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in task_by_id[task_id].depends_on:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task in tasks:
            if task.status is not TaskStatus.SUPERSEDED:
                visit(task.id)
