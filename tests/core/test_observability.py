import unittest

from core.observability import format_run_log


class ObservabilityContractTest(unittest.TestCase):
    def test_todo_revision_checkpoint_is_written_to_run_log(self):
        trace = {
            "started_at": "2026-01-01 00:00:00",
            "ended_at": "2026-01-01 00:00:01",
            "model": "test-model",
            "run_id": "run-1",
            "status": "completed",
            "question": "hello",
            "answer": "world",
            "checkpoints": [
                {
                    "kind": "tool_batch",
                    "reason": "tool_batch_completed",
                    "message": "TodoList 已同步。",
                    "data": {
                        "todo_list": {
                            "phase": "active",
                            "sync_state": "clean",
                            "revision": 2,
                        },
                    },
                }
            ],
        }

        log = format_run_log(trace)

        self.assertIn('"reason": "tool_batch_completed"', log)
        self.assertIn('"todo_list": {', log)
        self.assertIn('"sync_state": "clean"', log)
        self.assertIn('"revision": 2', log)

    def test_missing_internal_trace_field_is_not_defaulted(self):
        trace = {
            "started_at": "2026-01-01 00:00:00",
            "ended_at": "2026-01-01 00:00:01",
            "model": "test-model",
            "run_id": "run-1",
            "status": "completed",
            "question": "hello",
            "answer": "world",
        }

        with self.assertRaises(KeyError):
            format_run_log(trace)


if __name__ == "__main__":
    unittest.main()
