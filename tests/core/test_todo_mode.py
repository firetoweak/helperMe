import json
import unittest

from core.model_call import LLMResponse, ToolCall
from core.todos import TodoMode, TodoPhase, TodoSyncState
from core.tools_runtime.tools_state import ToolStep


class TodoModeTest(unittest.TestCase):
    @staticmethod
    def _snapshot(todos, *, reason="同步") -> str:
        return json.dumps(
            {"objective": "目标", "reason": reason, "todos": todos},
            ensure_ascii=False,
        )

    @classmethod
    def _active_mode(cls):
        mode = TodoMode()
        state = mode.create_state()
        mode.accept_start_response(
            state,
            LLMResponse(
                type="tool_calls",
                calls=[
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
                ],
            ),
        )
        return mode, state

    def test_state_is_created_per_run_instead_of_stored_on_mode(self):
        mode = TodoMode()

        first = mode.create_state()
        second = mode.create_state()

        self.assertIsNot(first, second)
        self.assertFalse(hasattr(mode, "todo_list"))

    def test_rewrite_todos_is_a_runtime_cognitive_tool(self):
        mode, state = self._active_mode()

        result = mode.execute_tool(
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

    def test_external_batch_marks_dirty_but_rewrite_only_batch_does_not(self):
        mode, state = self._active_mode()
        external = ToolStep("call-1", "read_file", "{}")
        rewrite = ToolStep("call-2", "rewrite_todos", "{}")

        mode.after_tool_batch(state, [external])
        self.assertEqual(state.sync_state, TodoSyncState.DIRTY)

        mode.execute_tool(
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

    def test_exit_barrier_returns_feedback_without_mutating_conversation(self):
        mode, state = self._active_mode()

        feedback = mode.check_final_candidate(state)
        self.assertIn("id=[1, 2]", feedback)

        mode.execute_tool(
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


if __name__ == "__main__":
    unittest.main()
