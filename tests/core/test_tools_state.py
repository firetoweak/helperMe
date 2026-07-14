import unittest

from core.tools_runtime.tools_state import ToolsState


class ToolsStateTest(unittest.TestCase):
    def test_result_is_the_single_source_for_step_properties(self):
        state = ToolsState()
        step = state.add_call("call-1", "demo", "{}")

        state.add_result(
            "call-1",
            {
                "ok": False,
                "code": "DEMO_ERROR",
                "data": None,
                "error": "failed",
                "hint": None,
            },
        )

        self.assertFalse(step.ok)
        self.assertEqual(step.code, "DEMO_ERROR")
        self.assertEqual(step.error, "failed")

    def test_result_cannot_be_recorded_twice(self):
        state = ToolsState()
        state.add_call("call-1", "demo", "{}")
        result = {"ok": True, "code": "OK", "data": None, "error": None}
        state.add_result("call-1", result)

        with self.assertRaises(ValueError):
            state.add_result("call-1", result)

    def test_summary_contains_no_derived_balanced_field(self):
        state = ToolsState()

        self.assertEqual(
            state.summary(),
            {"total": 0, "pending": 0, "failed": 0},
        )


if __name__ == "__main__":
    unittest.main()
