from __future__ import annotations

from typing import Protocol

from plugins.goal.goal import Goal, GoalStatus


class GoalStore(Protocol):
    def add(self, session_id: str, goal: Goal) -> None:
        ...

    def get(self, goal_id: str) -> Goal:
        ...

    def session_id_for(self, goal_id: str) -> str:
        ...

    def active_for_session(self, session_id: str) -> Goal | None:
        ...


class InMemoryGoalStore:
    def __init__(self) -> None:
        self._goals: dict[str, Goal] = {}
        self._goal_sessions: dict[str, str] = {}
        self._session_goal_ids: dict[str, list[str]] = {}

    def add(self, session_id: str, goal: Goal) -> None:
        if not session_id.strip():
            raise ValueError("session_id cannot be empty")
        if goal.id in self._goals:
            raise ValueError(f"duplicate goal_id: {goal.id}")
        active = self.active_for_session(session_id)
        if active is not None:
            raise ValueError(
                f"session {session_id} already has active goal: {active.id}"
            )

        self._goals[goal.id] = goal
        self._goal_sessions[goal.id] = session_id
        self._session_goal_ids.setdefault(session_id, []).append(goal.id)

    def get(self, goal_id: str) -> Goal:
        return self._goals[goal_id]

    def session_id_for(self, goal_id: str) -> str:
        return self._goal_sessions[goal_id]

    def active_for_session(self, session_id: str) -> Goal | None:
        return next(
            (
                self._goals[goal_id]
                for goal_id in reversed(
                    self._session_goal_ids.get(session_id, [])
                )
                if self._goals[goal_id].status
                not in {GoalStatus.COMPLETED, GoalStatus.EXHAUSTED}
            ),
            None,
        )
