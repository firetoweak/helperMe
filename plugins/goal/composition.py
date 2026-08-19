from __future__ import annotations

from core.turn_host import TurnHost
from plugins.goal.application import GoalApplicationService
from plugins.goal.store import InMemoryGoalStore


def create_goal_plugin(
    turn_host: TurnHost,
    *,
    default_max_turns: int,
) -> GoalApplicationService:
    return GoalApplicationService(
        turn_host,
        InMemoryGoalStore(),
        default_max_turns=default_max_turns,
    )
