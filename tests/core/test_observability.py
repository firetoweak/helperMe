import unittest

from core.observability import format_run_log


class ObservabilityContractTest(unittest.TestCase):
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
