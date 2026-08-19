import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from jsonschema.exceptions import SchemaError

from core.tool_registry import (
    EmptyInput,
    JsonSchemaParameters,
    PydanticParameters,
    ToolRegistry,
    ToolSpec,
)
from core.tools_runtime.stop_guard import evaluate_stop_safety
from core.tools_runtime.tools_executor import ToolsExecutor, normalize_tool_result
from core.tools_runtime.tools_protocol import (
    build_tool_messages,
    validate_tool_message_chain,
)
from core.tools_runtime.tools_state import ToolsState
from tools.get_changes import GetChangesInput, _run_git, create_get_changes_specs
from tools.workspace import WorkspaceSandbox, WorkspaceSandboxes


SUCCESS = {"ok": True, "code": "OK", "data": None, "error": None}
COMMAND_COMPLETED = {
    "ok": True,
    "code": "COMMAND_COMPLETED",
    "data": None,
    "error": None,
}
COMMAND_TIMEOUT = {
    "ok": False,
    "code": "COMMAND_TIMEOUT",
    "data": None,
    "error": "timeout",
}


def command_result(code, workspace_effect):
    return {
        "ok": code == "COMMAND_COMPLETED",
        "code": code,
        "data": {"workspace_effect": workspace_effect},
        "error": None if code == "COMMAND_COMPLETED" else "timeout",
    }


class ToolRegistryEarlyFailTest(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_registration_fails_without_replacing_original(self):
        tool_name = "duplicate_registration_test_tool"

        def original_handler(_):
            return {"ok": True, "code": "ORIGINAL"}

        registry = ToolRegistry()
        original = ToolSpec(
            tool_name,
            "original",
            PydanticParameters(EmptyInput),
            original_handler,
        )
        registry.register(original)

        with self.assertRaises(ValueError):
            registry.register(
                ToolSpec(
                    tool_name,
                    "replacement",
                    PydanticParameters(EmptyInput),
                    original_handler,
                )
            )

        self.assertIs(registry.get(tool_name), original)

    async def test_invalid_json_schema_fails_during_parameters_creation(self):
        with self.assertRaises(SchemaError):
            JsonSchemaParameters({"type": "not-a-json-schema-type"})

    async def test_non_object_json_schema_fails_during_parameters_creation(self):
        for schema in ({"type": "array"}, {}):
            with self.subTest(schema=schema):
                with self.assertRaisesRegex(ValueError, "顶层 type.*object"):
                    JsonSchemaParameters(schema)


class JsonSchemaToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.schema = {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "days": {"type": "integer", "minimum": 1},
            },
            "required": ["city"],
            "additionalProperties": False,
        }
        self.received_arguments = []
        self.registry = ToolRegistry()
        self.registry.register(
            ToolSpec(
                name="external_weather",
                description="external JSON Schema tool",
                parameters=JsonSchemaParameters(self.schema),
                handler=self._handle,
            )
        )
        self.executor = ToolsExecutor(self.registry)

    async def _handle(self, arguments):
        self.received_arguments.append(arguments)
        return {"ok": True, "code": "OK", "data": arguments}

    async def test_exposes_original_schema_and_passes_original_dict_to_handler(self):
        exposed_schema = self.registry.get_tools()[0]["function"]["parameters"]
        arguments = {"city": "北京", "days": 2}

        result = await self.executor.execute(
            "external_weather",
            json.dumps(arguments, ensure_ascii=False),
        )

        self.assertEqual(exposed_schema, self.schema)
        self.assertTrue(result["ok"])
        self.assertEqual(self.received_arguments, [arguments])

    async def test_schema_validation_failure_is_recoverable_tool_error(self):
        result = await self.executor.execute(
            "external_weather",
            json.dumps({"days": 0}),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VALIDATION_ERROR")
        self.assertEqual(self.received_arguments, [])

    async def test_non_object_arguments_are_recoverable_tool_error(self):
        result = await self.executor.execute("external_weather", "[]")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VALIDATION_ERROR")
        self.assertEqual(self.received_arguments, [])

    async def test_schema_is_snapshotted_without_normalization(self):
        parameters = JsonSchemaParameters(self.schema)
        self.schema["properties"]["city"]["type"] = "integer"

        self.assertEqual(
            parameters.schema()["properties"]["city"]["type"],
            "string",
        )


class ToolsExecutorEarlyFailTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.executor = ToolsExecutor(self.registry)

    def register(self, name, handler):
        self.registry.register(
            ToolSpec(name, "test tool", PydanticParameters(EmptyInput), handler)
        )

    async def test_preserves_explicit_success_and_failure(self):
        success = normalize_tool_result(
            {"ok": True, "code": "READ_OK", "value": 1}
        )
        failure = normalize_tool_result(
            {"ok": False, "code": "READ_FAILED", "error": "failed"}
        )

        self.assertEqual(success["data"], {"value": 1})
        self.assertEqual(failure["error"], "failed")

    async def test_missing_tool_reports_current_turn_without_guessing_source(self):
        result = await self.executor.execute("echo", "{}")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "TOOL_NOT_FOUND")
        self.assertIn("current Turn", result["error"])
        self.assertIn("load_toolset", result["hint"])
        self.assertNotIn("MCP", result["error"])

    async def test_empty_arguments_fail_even_for_no_arg_tool(self):
        tool_name = "early_fail_no_arg_tool"

        async def handler(_):
            return {"ok": True, "code": "OK"}

        self.register(tool_name, handler)
        result = await self.executor.execute(tool_name, "")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_JSON")

    async def test_no_arg_tool_accepts_explicit_empty_object(self):
        tool_name = "explicit_no_arg_tool"

        async def handler(_):
            return {"ok": True, "code": "OK"}

        self.register(tool_name, handler)
        result = await self.executor.execute(tool_name, "{}")

        self.assertTrue(result["ok"])

    async def test_handler_bug_is_not_converted_to_tool_failure(self):
        tool_name = "crashing_internal_tool"

        async def handler(_):
            raise RuntimeError("handler bug")

        self.register(tool_name, handler)
        with self.assertRaisesRegex(RuntimeError, "handler bug"):
            await self.executor.execute(tool_name, "{}")

    async def test_async_handler_is_awaited_without_exception_wrapping(self):
        tool_name = "async_boundary_tool"
        entered = False

        async def handler(_):
            nonlocal entered
            entered = True
            return {"ok": True, "code": "ASYNC_OK"}

        self.register(tool_name, handler)

        result = await self.executor.execute(tool_name, "{}")

        self.assertTrue(entered)
        self.assertEqual(result["code"], "ASYNC_OK")


class ToolsStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_result_is_the_single_source_for_step_properties(self):
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

    async def test_result_cannot_be_recorded_twice(self):
        state = ToolsState()
        state.add_call("call-1", "demo", "{}")
        result = {"ok": True, "code": "OK", "data": None, "error": None}
        state.add_result("call-1", result)

        with self.assertRaises(ValueError):
            state.add_result("call-1", result)

    async def test_summary_contains_no_derived_balanced_field(self):
        state = ToolsState()

        self.assertEqual(
            state.summary(),
            {"total": 0, "pending": 0, "failed": 0},
        )


class ToolsProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_tool_chain_is_valid(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1"}],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "{}",
            },
        ]

        self.assertTrue(validate_tool_message_chain(messages).ok)

    async def test_dangling_tool_call_is_invalid(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1"}],
            }
        ]

        result = validate_tool_message_chain(messages)

        self.assertFalse(result.ok)
        self.assertEqual(result.pending_tool_call_ids, ["call-1"])

    async def test_build_tool_messages_only_exports_completed_steps(self):
        state = ToolsState()
        completed = state.add_call("call-1", "demo", "{}")
        state.add_call("call-2", "demo", "{}")
        state.add_result(
            "call-1",
            {"ok": True, "code": "OK", "data": None, "error": None},
        )

        messages = build_tool_messages([completed, state.get_step("call-2")], json.dumps)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["tool_call_id"], "call-1")


class StopGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_turn_without_writes_can_stop(self):
        safety = evaluate_stop_safety([], ToolsState())

        self.assertTrue(safety.can_stop)

    async def test_successful_write_requires_verification(self):
        state = ToolsState()
        state.add_call("write-1", "write_file", "{}")
        state.add_result("write-1", SUCCESS)

        safety = evaluate_stop_safety([], state)

        self.assertTrue(safety.protocol_safe)
        self.assertFalse(safety.business_safe)
        self.assertEqual(safety.reason, "verification_required")

    async def test_verification_after_last_write_allows_stop(self):
        state = ToolsState()
        state.add_call("write-1", "write_file", "{}")
        state.add_result("write-1", SUCCESS)
        state.add_call("verify-1", "get_changes", "{}")
        state.add_result("verify-1", SUCCESS)

        self.assertTrue(evaluate_stop_safety([], state).can_stop)

    async def test_new_write_after_verification_requires_new_verification(self):
        state = ToolsState()
        for call_id, name in [
            ("write-1", "write_file"),
            ("verify-1", "get_changes"),
            ("write-2", "apply_patch"),
        ]:
            state.add_call(call_id, name, "{}")
            state.add_result(call_id, SUCCESS)

        self.assertFalse(evaluate_stop_safety([], state).can_stop)

    async def test_may_write_commands_require_verification(self):
        for code, result in (
            ("COMMAND_COMPLETED", COMMAND_COMPLETED),
            ("COMMAND_TIMEOUT", COMMAND_TIMEOUT),
        ):
            with self.subTest(code=code):
                state = ToolsState()
                state.add_call("command-1", "execute_command", "{}")
                state.add_result("command-1", result)

                self.assertFalse(evaluate_stop_safety([], state).can_stop)

    async def test_read_only_commands_do_not_require_verification(self):
        for code in ("COMMAND_COMPLETED", "COMMAND_TIMEOUT"):
            with self.subTest(code=code):
                state = ToolsState()
                state.add_call("command-1", "execute_command", "{}")
                state.add_result("command-1", command_result(code, "read_only"))

                self.assertTrue(evaluate_stop_safety([], state).can_stop)

    async def test_command_rejected_before_start_does_not_require_verification(self):
        state = ToolsState()
        state.add_call("command-1", "execute_command", "{}")
        state.add_result(
            "command-1",
            {"ok": False, "code": "EMPTY_COMMAND", "error": "empty"},
        )

        self.assertTrue(evaluate_stop_safety([], state).can_stop)


class GetChangesEarlyFailTest(unittest.IsolatedAsyncioTestCase):
    @patch("tools.get_changes.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    async def test_cancelled_git_process_is_reaped(self, create_process):
        entered = asyncio.Event()
        killed = asyncio.Event()

        class BlockingProcess:
            returncode = None

            async def communicate(self):
                entered.set()
                await asyncio.Event().wait()

            def kill(self):
                self.returncode = -9
                killed.set()

            async def wait(self):
                await killed.wait()
                return self.returncode

        create_process.return_value = BlockingProcess()
        task = asyncio.create_task(_run_git(["status"], Path.cwd()))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(killed.is_set())

    @patch("tools.get_changes._run_git", new_callable=AsyncMock)
    async def test_non_git_workspace_reports_verification_failure(self, run):
        run.return_value = (128, "", "not a repository")
        with tempfile.TemporaryDirectory() as directory:
            workspaces = WorkspaceSandboxes({
                "project": WorkspaceSandbox(Path(directory))
            })
            get_changes = create_get_changes_specs(workspaces)[0].handler

            result = await get_changes(GetChangesInput(root="project"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VERIFICATION_BACKEND_UNAVAILABLE")
        self.assertIsNone(result["changed"])


if __name__ == "__main__":
    unittest.main()
