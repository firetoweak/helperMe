from __future__ import annotations

from dataclasses import dataclass

from core.todos.todo_list import TodoList, TodoSyncState


@dataclass(frozen=True)
class TodoExitDecision:
    allowed: bool
    feedback: str | None = None


def check_todo_exit(todo_list: TodoList) -> TodoExitDecision:
    if todo_list.sync_state is TodoSyncState.DIRTY:
        return TodoExitDecision(
            allowed=False,
            feedback=(
                "Todo Sync Barrier 未通过：最近的外部工具结果尚未同步。"
                "你可以继续调用外部工具；准备结束本 Run 时，请先调用 "
                "rewrite_todos 提交完整最新快照。"
            ),
        )

    unfinished_ids = [
        item.id
        for item in todo_list.items
        if item.status in {"pending", "doing"}
    ]
    if unfinished_ids:
        return TodoExitDecision(
            allowed=False,
            feedback=(
                "Todo Sync Barrier 未通过：TodoList 中仍有未结束事项，"
                f"id={unfinished_ids}。请继续执行；确认不再需要的事项可标记为 "
                "cancelled，并在 note 中说明原因。同步完整快照后再结束本 Run。"
            ),
        )

    return TodoExitDecision(allowed=True)
