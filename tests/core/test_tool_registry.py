import unittest

from core.tool_registry import TOOL_SPECS, register_tool


class ToolRegistryEarlyFailTest(unittest.TestCase):
    def test_duplicate_registration_fails_without_replacing_original(self):
        tool_name = "duplicate_registration_test_tool"

        @register_tool("original")
        def duplicate_registration_test_tool(_):
            return {"ok": True, "code": "ORIGINAL"}

        original = TOOL_SPECS[tool_name]
        try:
            with self.assertRaises(ValueError):

                @register_tool("replacement")
                def duplicate_registration_test_tool(_):
                    return {"ok": True, "code": "REPLACEMENT"}

            self.assertIs(TOOL_SPECS[tool_name], original)
        finally:
            TOOL_SPECS.pop(tool_name, None)


if __name__ == "__main__":
    unittest.main()
