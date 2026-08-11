from __future__ import annotations

from typing import Callable
from uuid import uuid4

from plugins.goal.application import GoalApplicationService, GoalLoopOutcome


class GoalCommandError(ValueError):
    pass


class GoalConsoleAdapter:
    def __init__(
        self,
        service: GoalApplicationService,
        goal_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._service = service
        self._goal_id_factory = goal_id_factory or (
            lambda: f"goal-{uuid4().hex}"
        )

    def execute_if_handled(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
    ) -> GoalLoopOutcome | None:
        objective = self._goal_objective(user_message)
        active = self._service.active_goal(session_id)

        if objective is not None:
            if active is not None:
                raise GoalCommandError(f"当前 Goal 尚未结束：{active.id}")
            return self._service.start_goal(
                session_id,
                self._goal_id_factory(),
                run_id,
                objective,
            )

        if active is None:
            return None
        return self._service.continue_goal(
            session_id,
            active.id,
            run_id,
            user_message,
        )

    def request_pause(self, session_id: str) -> bool:
        return self._service.request_pause(session_id)

    @staticmethod
    def _goal_objective(user_message: str) -> str | None:
        parts = user_message.split(maxsplit=1)
        if not parts or parts[0] != "/goal":
            return None
        if len(parts) == 1 or not parts[1].strip():
            raise GoalCommandError("/goal 后必须提供目标")
        return parts[1].strip()
