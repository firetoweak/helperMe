import json
import unittest

from core.todos import (
    TodoList,
    TodoPhase,
    TodoSyncState,
    execute_rewrite_todos,
    rewrite_todos_tool_schema,
)


class RewriteTodosTest(unittest.TestCase):
    @staticmethod
    def _arguments(todos, *, objective="完成任务", reason="初始化") -> str:
        return json.dumps(
            {"objective": objective, "reason": reason, "todos": todos},
            ensure_ascii=False,
        )

    def test_first_call_initializes_todo_list(self):
        state = TodoList()

        result = execute_rewrite_todos(
            state,
            self._arguments(
                [
                    {"id": None, "content": "分析", "status": "pending"},
                    {"id": None, "content": "实现", "status": "pending"},
                ]
            ),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(state.phase, TodoPhase.ACTIVE)
        self.assertEqual(state.sync_state, TodoSyncState.CLEAN)
        self.assertEqual(state.revision, 1)
        self.assertEqual([item.id for item in state.items], [1, 2])

    def test_initialization_requires_null_ids_and_pending_status(self):
        invalid_todos = (
            [
                {"id": 1, "content": "分析", "status": "pending"},
                {"id": None, "content": "实现", "status": "pending"},
            ],
            [
                {"id": None, "content": "分析", "status": "doing"},
                {"id": None, "content": "实现", "status": "pending"},
            ],
        )
        for todos in invalid_todos:
            with self.subTest(todos=todos):
                result = execute_rewrite_todos(
                    TodoList(), self._arguments(todos)
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "INVALID_TODO_REWRITE")

    def test_cancelled_todo_requires_note(self):
        state = TodoList()
        result = execute_rewrite_todos(
            state,
            self._arguments(
                [
                    {"id": None, "content": "分析", "status": "pending"},
                    {"id": None, "content": "实现", "status": "pending"},
                ]
            ),
        )
        self.assertTrue(result["ok"])

        result = execute_rewrite_todos(
            state,
            self._arguments(
                [
                    {"id": 1, "content": "分析", "status": "done"},
                    {"id": 2, "content": "实现", "status": "cancelled"},
                ],
                reason="取消实现",
            ),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VALIDATION_ERROR")

    def test_schema_exposes_full_snapshot_contract(self):
        tool = rewrite_todos_tool_schema()

        self.assertEqual(tool["function"]["name"], "rewrite_todos")
        required = tool["function"]["parameters"]["required"]
        self.assertEqual(set(required), {"objective", "reason", "todos"})
        parameters = tool["function"]["parameters"]
        self.assertIn("RewriteTodoInput", parameters["$defs"])
        self.assertEqual(
            parameters["properties"]["todos"]["items"]["$ref"],
            "#/$defs/RewriteTodoInput",
        )


if __name__ == "__main__":
    unittest.main()
