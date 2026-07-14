import unittest

from core.tools_runtime.stop_guard import evaluate_stop_safety
from core.tools_runtime.tools_state import ToolsState


SUCCESS = {"ok": True, "code": "OK", "data": None, "error": None}


class StopGuardTest(unittest.TestCase):
    def test_run_without_writes_can_stop(self):
        safety = evaluate_stop_safety([], ToolsState())

        self.assertTrue(safety.can_stop)

    def test_successful_write_requires_verification(self):
        state = ToolsState()
        state.add_call("write-1", "write_file", "{}")
        state.add_result("write-1", SUCCESS)

        safety = evaluate_stop_safety([], state)

        self.assertTrue(safety.protocol_safe)
        self.assertFalse(safety.business_safe)
        self.assertEqual(safety.reason, "verification_required")

    def test_verification_after_last_write_allows_stop(self):
        state = ToolsState()
        state.add_call("write-1", "write_file", "{}")
        state.add_result("write-1", SUCCESS)
        state.add_call("verify-1", "get_changes", "{}")
        state.add_result("verify-1", SUCCESS)

        self.assertTrue(evaluate_stop_safety([], state).can_stop)

    def test_new_write_after_verification_requires_new_verification(self):
        state = ToolsState()
        for call_id, name in [
            ("write-1", "write_file"),
            ("verify-1", "get_changes"),
            ("write-2", "apply_patch"),
        ]:
            state.add_call(call_id, name, "{}")
            state.add_result(call_id, SUCCESS)

        self.assertFalse(evaluate_stop_safety([], state).can_stop)


if __name__ == "__main__":
    unittest.main()
