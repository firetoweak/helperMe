from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


TodoStatus = Literal["pending", "doing", "done", "cancelled"]


class TodoPhase(str, Enum):
    UNINITIALIZED = "uninitialized"
    ACTIVE = "active"
    COMPLETED = "completed"


class TodoSyncState(str, Enum):
    CLEAN = "clean"
    DIRTY = "dirty"


@dataclass(frozen=True)
class TodoDraft:
    id: int | None
    content: str
    status: TodoStatus
    note: str | None = None


@dataclass
class TodoItem:
    id: int
    content: str
    status: TodoStatus = "pending"
    note: str | None = None


class TodoList:
    def __init__(self) -> None:
        self.objective: str | None = None
        self.items: list[TodoItem] = []
        self.revision = 0
        self.phase = TodoPhase.UNINITIALIZED
        self.sync_state: TodoSyncState | None = None
        self._next_id = 1

    def mark_dirty(self) -> None:
        if self.phase is not TodoPhase.ACTIVE:
            raise ValueError("only an active todo list can become dirty")
        self.sync_state = TodoSyncState.DIRTY

    def apply_snapshot(self, objective: str, drafts: list[TodoDraft]) -> bool:
        if self.phase is TodoPhase.COMPLETED:
            raise ValueError("completed todo list cannot be rewritten")

        if self.phase is TodoPhase.UNINITIALIZED:
            if not 2 <= len(drafts) <= 6:
                raise ValueError("initial todo list must contain 2 to 6 items")
            if any(draft.id is not None for draft in drafts):
                raise ValueError("initial todo ids must be null")
            if any(draft.status != "pending" for draft in drafts):
                raise ValueError("initial todo status must be pending")

        known_ids = {item.id for item in self.items}
        submitted_ids = [draft.id for draft in drafts if draft.id is not None]
        if len(submitted_ids) != len(set(submitted_ids)):
            raise ValueError("todo ids must not be duplicated")
        unknown_ids = set(submitted_ids) - known_ids
        if unknown_ids:
            raise ValueError(f"unknown todo ids: {sorted(unknown_ids)}")
        if sum(draft.status == "doing" for draft in drafts) > 1:
            raise ValueError("todo list can contain at most one doing item")

        rewritten: list[TodoItem] = []
        for draft in drafts:
            item_id = draft.id
            if item_id is None:
                item_id = self._next_id
                self._next_id += 1
            rewritten.append(
                TodoItem(
                    id=item_id,
                    content=draft.content,
                    status=draft.status,
                    note=draft.note,
                )
            )

        changed = objective != self.objective or rewritten != self.items
        self.objective = objective
        self.items = rewritten
        if self.phase is TodoPhase.UNINITIALIZED:
            self.phase = TodoPhase.ACTIVE
            self.revision = 1
        elif changed:
            self.revision += 1
        self.sync_state = TodoSyncState.CLEAN
        return changed

    def complete(self) -> None:
        if self.phase is not TodoPhase.ACTIVE:
            raise ValueError("only an active todo list can be completed")
        if self.sync_state is not TodoSyncState.CLEAN:
            raise ValueError("dirty todo list cannot be completed")
        if any(item.status in {"pending", "doing"} for item in self.items):
            raise ValueError("active todo items must be resolved before completion")
        self.phase = TodoPhase.COMPLETED

    def to_dict(self) -> dict:
        return {
            "objective": self.objective,
            "revision": self.revision,
            "phase": self.phase.value,
            "sync_state": (
                self.sync_state.value if self.sync_state is not None else None
            ),
            "todos": [
                {
                    "id": item.id,
                    "content": item.content,
                    "status": item.status,
                    "note": item.note,
                }
                for item in self.items
            ],
        }
