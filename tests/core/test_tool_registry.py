import unittest

from core.tool_registry import EmptyInput, ToolRegistry, ToolSpec


class ToolRegistryEarlyFailTest(unittest.TestCase):
    def test_duplicate_registration_fails_without_replacing_original(self):
        tool_name = "duplicate_registration_test_tool"

        def original_handler(_):
            return {"ok": True, "code": "ORIGINAL"}

        registry = ToolRegistry()
        original = ToolSpec(
            tool_name,
            "original",
            EmptyInput,
            original_handler,
        )
        registry.register(original)

        with self.assertRaises(ValueError):
            registry.register(
                ToolSpec(
                    tool_name,
                    "replacement",
                    EmptyInput,
                    original_handler,
                )
            )

        self.assertIs(registry.get(tool_name), original)


if __name__ == "__main__":
    unittest.main()
