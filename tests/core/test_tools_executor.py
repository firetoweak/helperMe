import unittest

from core.tool_registry import EmptyInput, ToolRegistry, ToolSpec
from core.tools_runtime.tools_executor import ToolsExecutor, normalize_tool_result


class ToolsExecutorEarlyFailTest(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.executor = ToolsExecutor(self.registry)

    def register(self, name, handler):
        self.registry.register(
            ToolSpec(name, "test tool", EmptyInput, handler)
        )

    def test_preserves_explicit_success_and_failure(self):
        success = normalize_tool_result(
            {"ok": True, "code": "READ_OK", "value": 1}
        )
        failure = normalize_tool_result(
            {"ok": False, "code": "READ_FAILED", "error": "failed"}
        )

        self.assertEqual(success["data"], {"value": 1})
        self.assertEqual(failure["error"], "failed")

    def test_empty_arguments_fail_even_for_no_arg_tool(self):
        tool_name = "early_fail_no_arg_tool"

        def handler(_):
            return {"ok": True, "code": "OK"}

        self.register(tool_name, handler)
        result = self.executor.execute(tool_name, "")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_JSON")

    def test_no_arg_tool_accepts_explicit_empty_object(self):
        tool_name = "explicit_no_arg_tool"

        def handler(_):
            return {"ok": True, "code": "OK"}

        self.register(tool_name, handler)
        result = self.executor.execute(tool_name, "{}")

        self.assertTrue(result["ok"])

    def test_handler_bug_is_not_converted_to_tool_failure(self):
        tool_name = "crashing_internal_tool"

        def handler(_):
            raise RuntimeError("handler bug")

        self.register(tool_name, handler)
        with self.assertRaisesRegex(RuntimeError, "handler bug"):
            self.executor.execute(tool_name, "{}")


if __name__ == "__main__":
    unittest.main()
