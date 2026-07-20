from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

ReplanAction = Literal["keep", "revise"]

MIN_REPLAN_STEPS = 1
MAX_REPLAN_STEPS = 6


class InvalidReplanResponse(ValueError):
    pass


@dataclass(frozen=True)
class ReplanDecision:
    action: ReplanAction
    reason: str
    steps: list[str]


def parse_replan_response(content: str) -> ReplanDecision:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidReplanResponse(
            f"replan response returned invalid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise InvalidReplanResponse("replan response must be a JSON object")

    action = payload.get("action")
    if action not in ("keep", "revise"):
        raise InvalidReplanResponse(
            "replan action must be 'keep' or 'revise'"
        )

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidReplanResponse(
            "replan reason must be a non-empty string"
        )
    reason = reason.strip()

    raw_steps = payload.get("steps")

    if action == "keep":
        if raw_steps is None or raw_steps == []:
            return ReplanDecision(action="keep", reason=reason, steps=[])
        raise InvalidReplanResponse(
            "replan keep must not include non-empty steps"
        )

    if not isinstance(raw_steps, list):
        raise InvalidReplanResponse("replan steps must be a list")

    steps: list[str] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, str) or not item.strip():
            raise InvalidReplanResponse(
                f"replan step[{index}] must be a non-empty string"
            )
        steps.append(item.strip())

    if len(steps) < MIN_REPLAN_STEPS:
        raise InvalidReplanResponse(
            f"replan must return at least {MIN_REPLAN_STEPS} steps"
        )
    if len(steps) > MAX_REPLAN_STEPS:
        raise InvalidReplanResponse(
            f"replan must return at most {MAX_REPLAN_STEPS} steps"
        )

    return ReplanDecision(action="revise", reason=reason, steps=steps)