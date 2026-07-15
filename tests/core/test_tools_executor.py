import unittest

from core.tool_registry import EmptyInput, TOOL_SPECS, register_tool
from core.tools_runtime.tools_executor import execute_tool, normalize_tool_result


class ToolsExecutorEarlyFailTest(unittest.TestCase):
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

        @register_tool("test tool", input_model=EmptyInput)
        def early_fail_no_arg_tool(_):
            return {"ok": True, "code": "OK"}

        try:
            result = execute_tool(tool_name, "")
        finally:
            TOOL_SPECS.pop(tool_name, None)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_JSON")

    def test_no_arg_tool_accepts_explicit_empty_object(self):
        tool_name = "explicit_no_arg_tool"

        @register_tool("test tool", input_model=EmptyInput)
        def explicit_no_arg_tool(_):
            return {"ok": True, "code": "OK"}

        try:
            result = execute_tool(tool_name, "{}")
        finally:
            TOOL_SPECS.pop(tool_name, None)

        self.assertTrue(result["ok"])

    def test_handler_bug_is_not_converted_to_tool_failure(self):
        tool_name = "crashing_internal_tool"

        @register_tool("test tool", input_model=EmptyInput)
        def crashing_internal_tool(_):
            raise RuntimeError("handler bug")

        try:
            with self.assertRaisesRegex(RuntimeError, "handler bug"):
                execute_tool(tool_name, "{}")
        finally:
            TOOL_SPECS.pop(tool_name, None)


if __name__ == "__main__":
    unittest.main()
