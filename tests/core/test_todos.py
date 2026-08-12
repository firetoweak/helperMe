import json
import unittest

from core.model_call import LLMResponse, ToolCall
from core.todos import (
    TodoDraft,
    TodoList,
    TodoMode,
    TodoPhase,
    TodoSyncState,
    execute_rewrite_todos,
    rewrite_todos_tool_schema,
)
from core.tools_runtime.tools_state import ToolStep


class TodoListTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _initialize(todos: TodoList) -> None:
        todos.apply_snapshot(
            "完成任务",
            [
                TodoDraft(None, "分析", "pending"),
                TodoDraft(None, "实现", "pending"),
            ],
        )

    async def test_lifecycle_and_sync_state_are_independent(self):
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

    async def test_rewrite_supports_update_add_delete_reorder_and_stable_ids(self):
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

    async def test_unchanged_rewrite_cleans_without_incrementing_revision(self):
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

    async def test_dirty_or_unresolved_todo_list_cannot_complete(self):
        todos = TodoList()
        self._initialize(todos)

        with self.assertRaises(ValueError):
            todos.complete()

        todos.mark_dirty()
        with self.assertRaises(ValueError):
            todos.complete()

    async def test_rewrite_rejects_unknown_duplicate_ids_and_multiple_doing(self):
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


class TodoModeTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _snapshot(todos, *, reason="同步") -> str:
        return json.dumps(
            {"objective": "目标", "reason": reason, "todos": todos},
            ensure_ascii=False,
        )

    @classmethod
    async def _active_mode(cls):
        mode = TodoMode()
        state = mode.create_state()
        await mode.accept_start_response(
            state,
            LLMResponse(
                calls=(
                    ToolCall(
                        "call-init",
                        "rewrite_todos",
                        cls._snapshot(
                            [
                                {"id": None, "content": "分析", "status": "pending"},
                                {"id": None, "content": "实现", "status": "pending"},
                            ],
                            reason="初始化",
                        ),
                    )
                ,),
            ),
        )
        return mode, state

    async def test_state_is_created_per_run_instead_of_stored_on_mode(self):
        mode = TodoMode()

        first = mode.create_state()
        second = mode.create_state()

        self.assertIsNot(first, second)
        self.assertFalse(hasattr(mode, "todo_list"))

    async def test_rewrite_todos_is_a_runtime_cognitive_tool(self):
        mode, state = await self._active_mode()

        result = await mode.execute_tool(
            state,
            "rewrite_todos",
            self._snapshot(
                [
                    {"id": 1, "content": "分析", "status": "done", "note": "已完成"},
                    {"id": None, "content": "编码", "status": "doing"},
                    {"id": None, "content": "测试", "status": "pending"},
                ],
                reason="完成分析并拆分实现",
            ),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(state.revision, 2)
        self.assertEqual([item.id for item in state.items], [1, 3, 4])

    async def test_external_batch_marks_dirty_but_rewrite_only_batch_does_not(self):
        mode, state = await self._active_mode()
        external = ToolStep("call-1", "read_file", "{}")
        rewrite = ToolStep("call-2", "rewrite_todos", "{}")

        mode.after_tool_batch(state, [external])
        self.assertEqual(state.sync_state, TodoSyncState.DIRTY)

        await mode.execute_tool(
            state,
            "rewrite_todos",
            self._snapshot(
                [
                    {"id": 1, "content": "分析", "status": "pending"},
                    {"id": 2, "content": "实现", "status": "pending"},
                ]
            ),
        )
        mode.after_tool_batch(state, [rewrite])
        self.assertEqual(state.sync_state, TodoSyncState.CLEAN)

        mode.after_tool_batch(state, [rewrite, external])
        self.assertEqual(state.sync_state, TodoSyncState.DIRTY)

    async def test_exit_barrier_returns_feedback_without_mutating_conversation(self):
        mode, state = await self._active_mode()

        feedback = mode.check_final_candidate(state)
        self.assertIn("id=[1, 2]", feedback)

        await mode.execute_tool(
            state,
            "rewrite_todos",
            self._snapshot(
                [
                    {"id": 1, "content": "分析", "status": "done"},
                    {
                        "id": 2,
                        "content": "实现",
                        "status": "cancelled",
                        "note": "不再需要",
                    },
                ]
            ),
        )
        self.assertIsNone(mode.check_final_candidate(state))
        mode.on_run_completed(state)
        self.assertEqual(state.phase, TodoPhase.COMPLETED)


class RewriteTodosTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _arguments(todos, *, objective="完成任务", reason="初始化") -> str:
        return json.dumps(
            {"objective": objective, "reason": reason, "todos": todos},
            ensure_ascii=False,
        )

    async def test_first_call_initializes_todo_list(self):
        state = TodoList()

        result = await execute_rewrite_todos(
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

    async def test_initialization_requires_null_ids_and_pending_status(self):
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
                result = await execute_rewrite_todos(
                    TodoList(), self._arguments(todos)
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "INVALID_TODO_REWRITE")

    async def test_cancelled_todo_requires_note(self):
        state = TodoList()
        result = await execute_rewrite_todos(
            state,
            self._arguments(
                [
                    {"id": None, "content": "分析", "status": "pending"},
                    {"id": None, "content": "实现", "status": "pending"},
                ]
            ),
        )
        self.assertTrue(result["ok"])

        result = await execute_rewrite_todos(
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

    async def test_schema_exposes_full_snapshot_contract(self):
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
