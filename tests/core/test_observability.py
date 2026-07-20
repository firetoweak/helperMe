import unittest

from core.observability import format_run_log


class ObservabilityContractTest(unittest.TestCase):
    def test_plan_revision_checkpoint_is_written_to_run_log(self):
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
                    "kind": "planning",
                    "reason": "plan_revision_decided",
                    "message": "已根据工具失败修订执行计划。",
                    "data": {
                        "trigger": "tool_failure",
                        "action": "revise",
                        "reason": "原工具不存在",
                        "before_plan": {"revision": 1},
                        "after_plan": {"revision": 2},
                    },
                }
            ],
        }

        log = format_run_log(trace)

        self.assertIn('"reason": "plan_revision_decided"', log)
        self.assertIn('"reason": "原工具不存在"', log)
        self.assertIn('"before_plan": {', log)
        self.assertIn('"revision": 1', log)
        self.assertIn('"after_plan": {', log)
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
