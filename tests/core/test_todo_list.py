import unittest

from core.todos import (
    TodoDraft,
    TodoList,
    TodoPhase,
    TodoSyncState,
)


class TodoListTest(unittest.TestCase):
    @staticmethod
    def _initialize(todos: TodoList) -> None:
        todos.apply_snapshot(
            "完成任务",
            [
                TodoDraft(None, "分析", "pending"),
                TodoDraft(None, "实现", "pending"),
            ],
        )

    def test_lifecycle_and_sync_state_are_independent(self):
        todos = TodoList()

        self.assertEqual(todos.phase, TodoPhase.UNINITIALIZED)
        self.assertIsNone(todos.sync_state)

        self._initialize(todos)
        self.assertEqual(todos.phase, TodoPhase.ACTIVE)
        self.assertEqual(todos.sync_state, TodoSyncState.CLEAN)

        todos.mark_dirty()
        self.assertEqual(todos.phase, TodoPhase.ACTIVE)
        self.assertEqual(todos.sync_state, TodoSyncState.DIRTY)

        todos.apply_snapshot(
            "完成任务",
            [
                TodoDraft(1, "分析", "done", "已分析"),
                TodoDraft(2, "实现", "cancelled", "不再需要"),
            ]
        )
        todos.complete()

        self.assertEqual(todos.phase, TodoPhase.COMPLETED)
        self.assertEqual(todos.sync_state, TodoSyncState.CLEAN)

    def test_rewrite_supports_update_add_delete_reorder_and_stable_ids(self):
        todos = TodoList()
        todos.apply_snapshot(
            "完成任务",
            [
                TodoDraft(None, "分析", "pending"),
                TodoDraft(None, "旧方案", "pending"),
                TodoDraft(None, "验证", "pending"),
            ],
        )

        changed = todos.apply_snapshot(
            "完成任务",
            [
                TodoDraft(3, "验证新方案", "pending"),
                TodoDraft(1, "深入分析", "done", "已完成"),
                TodoDraft(None, "实现新方案", "doing"),
            ]
        )

        self.assertTrue(changed)
        self.assertEqual(todos.revision, 2)
        self.assertEqual(
            [(item.id, item.content, item.status) for item in todos.items],
            [
                (3, "验证新方案", "pending"),
                (1, "深入分析", "done"),
                (4, "实现新方案", "doing"),
            ],
        )

        todos.apply_snapshot(
            "完成任务",
            [
                TodoDraft(4, "实现新方案", "done"),
                TodoDraft(None, "最终检查", "doing"),
            ]
        )
        self.assertEqual([item.id for item in todos.items], [4, 5])

    def test_unchanged_rewrite_cleans_without_incrementing_revision(self):
        todos = TodoList()
        self._initialize(todos)
        todos.mark_dirty()

        changed = todos.apply_snapshot(
            "完成任务",
            [
                TodoDraft(1, "分析", "pending"),
                TodoDraft(2, "实现", "pending"),
            ]
        )

        self.assertFalse(changed)
        self.assertEqual(todos.revision, 1)
        self.assertEqual(todos.sync_state, TodoSyncState.CLEAN)

    def test_dirty_or_unresolved_todo_list_cannot_complete(self):
        todos = TodoList()
        self._initialize(todos)

        with self.assertRaises(ValueError):
            todos.complete()

        todos.mark_dirty()
        with self.assertRaises(ValueError):
            todos.complete()

    def test_rewrite_rejects_unknown_duplicate_ids_and_multiple_doing(self):
        todos = TodoList()
        self._initialize(todos)

        invalid_drafts = (
            [TodoDraft(9, "未知", "pending")],
            [
                TodoDraft(1, "分析", "pending"),
                TodoDraft(1, "重复", "done"),
            ],
            [
                TodoDraft(1, "分析", "doing"),
                TodoDraft(2, "实现", "doing"),
            ],
        )
        for drafts in invalid_drafts:
            with self.subTest(drafts=drafts):
                with self.assertRaises(ValueError):
                    todos.apply_snapshot("完成任务", drafts)


if __name__ == "__main__":
    unittest.main()
