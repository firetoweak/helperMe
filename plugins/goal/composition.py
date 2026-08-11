from __future__ import annotations

from core.run_host import RunHost
from plugins.goal.application import GoalApplicationService
from plugins.goal.store import InMemoryGoalStore


def create_goal_plugin(
    run_host: RunHost,
    *,
    default_max_turns: int,
) -> GoalApplicationService:
    return GoalApplicationService(
        run_host,
        InMemoryGoalStore(),
        default_max_turns=default_max_turns,
    )
