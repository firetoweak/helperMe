from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PlanStepStatus = Literal["pending", "doing", "done", "skipped"]


@dataclass
class PlanStep:
    id: int
    text: str
    status: PlanStepStatus = "pending"
    note: str | None = None


@dataclass
class Plan:
    goal: str
    steps: list[PlanStep]

    def get_step(self, step_id: int) -> PlanStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise ValueError(f"plan step not found: {step_id}")

    def current_step(self) -> PlanStep | None:
        for step in self.steps:
            if step.status == "doing":
                return step
        for step in self.steps:
            if step.status == "pending":
                return step
        return None

    def mark_doing(self, step_id: int, note: str | None = None) -> PlanStep:
        step = self.get_step(step_id)
        step.status = "doing"
        step.note = note
        return step

    def mark_done(self, step_id: int, note: str | None = None) -> PlanStep:
        step = self.get_step(step_id)
        step.status = "done"
        step.note = note
        return step

    def mark_skipped(self, step_id: int, note: str | None = None) -> PlanStep:
        step = self.get_step(step_id)
        step.status = "skipped"
        step.note = note
        return step

    def advance_to_next(self, done_note: str | None = None, doing_note: str | None = None) -> None:
        current = self.current_step()
        if current and current.status == "doing":
            current.status = "done"
            current.note = done_note

        next_step = self.current_step()
        if next_step and next_step.status == "pending":
            next_step.status = "doing"
            next_step.note = doing_note

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "steps": [
                {
                    "id": step.id,
                    "text": step.text,
                    "status": step.status,
                    "note": step.note,
                }
                for step in self.steps
            ],
        }