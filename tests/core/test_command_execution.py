from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from pydantic import ValidationError

from core.tool_registry import ToolRegistry
from core.tools_runtime.tools_executor import ToolsExecutor
from tools.command_execution import (
    ExecuteCommandInput,
    create_command_execution_spec,
)
from tools.powershell_runner import (
    CaptureLimit,
    CommandEnvironmentPolicy,
    PowerShellCommandRunner,
)
from tools.workspace import WorkspaceSandbox, WorkspaceSandboxes


POWERSHELL = shutil.which("powershell.exe")


class CommandEnvironmentPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_forwards_baseline_and_explicit_names(self):
        policy = CommandEnvironmentPolicy(
            forward_names=("HELPER_ALLOWED",),
            fixed_values={"HELPER_FIXED": "fixed"},
        )

        child_env = policy.build({
            "PATH": "bin",
            "HELPER_ALLOWED": "allowed",
            "HELPER_SECRET": "secret",
        })

        self.assertEqual(child_env["PATH"], "bin")
        self.assertEqual(child_env["HELPER_ALLOWED"], "allowed")
        self.assertEqual(child_env["HELPER_FIXED"], "fixed")
        self.assertNotIn("HELPER_SECRET", child_env)

    async def test_environment_name_matching_is_case_insensitive(self):
        policy = CommandEnvironmentPolicy(forward_names=("helper_allowed",))

        child_env = policy.build({"HELPER_ALLOWED": "yes"})

        self.assertEqual(child_env["HELPER_ALLOWED"], "yes")


@unittest.skipUnless(POWERSHELL, "需要 Windows PowerShell")
class PowerShellCommandRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_stdout_stderr_and_explicit_exit_code(self):
        runner = PowerShellCommandRunner()

        result = await runner.run(
            '[Console]::Out.Write("out"); '
            '[Console]::Error.Write("err"); exit 7',
            Path.cwd(),
            10,
        )

        self.assertEqual(result.exit_code, 7)
        self.assertEqual(result.stdout.content, "out")
        self.assertEqual(result.stderr.content, "err")
        self.assertFalse(result.timed_out)

    async def test_uses_powershell_composition_semantics(self):
        runner = PowerShellCommandRunner()

        result = await runner.run(
            'Write-Output "alpha" | ForEach-Object { "$_-beta" }',
            Path.cwd(),
            10,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.content.strip(), "alpha-beta")

    async def test_decodes_utf8_output(self):
        runner = PowerShellCommandRunner()
        expected = "".join(chr(code) for code in (0x4E2D, 0x6587))

        result = await runner.run(
            f'[Console]::Out.Write("{expected}")',
            Path.cwd(),
            10,
        )

        self.assertEqual(result.stdout.content, expected)

    async def test_does_not_inherit_unlisted_host_variable(self):
        runner = PowerShellCommandRunner()
        name = "HELPER_COMMAND_SECRET_TEST"
        previous = os.environ.get(name)
        os.environ[name] = "must-not-leak"
        try:
            result = await runner.run(
                f'if (Test-Path Env:{name}) {{ Write-Output "leaked" }} '
                'else { Write-Output "hidden" }',
                Path.cwd(),
                10,
            )
        finally:
            if previous is None:
                del os.environ[name]
            else:
                os.environ[name] = previous

        self.assertEqual(result.stdout.content.strip(), "hidden")

    async def test_large_output_keeps_head_and_tail(self):
        runner = PowerShellCommandRunner(
            capture_limit=CaptureLimit(max_chars=100, head_chars=40)
        )

        result = await runner.run(
            '[Console]::Out.Write(("A" * 8000) + "TAIL")',
            Path.cwd(),
            10,
        )

        self.assertTrue(result.stdout.truncated)
        self.assertEqual(result.stdout.total_chars, 8004)
        self.assertEqual(result.stdout.omitted_chars, 7904)
        self.assertTrue(result.stdout.content.startswith("A" * 40))
        self.assertTrue(result.stdout.content.endswith("TAIL"))

    async def test_timeout_returns_partial_result_and_stops_waiting(self):
        runner = PowerShellCommandRunner()

        result = await runner.run(
            'Write-Output "started"; Start-Sleep -Seconds 5',
            Path.cwd(),
            0.2,
        )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertIn("started", result.stdout.content)
        self.assertLess(result.duration_ms, 4_000)

    async def test_timeout_terminates_child_process_tree(self):
        runner = PowerShellCommandRunner()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "child-finished.txt"
            script = root / "child.ps1"
            script.write_text(
                "Start-Sleep -Seconds 2\n"
                f"Set-Content -LiteralPath '{marker}' -Value done\n",
                encoding="utf-8",
            )

            result = await runner.run(
                f"& powershell.exe -NoProfile -File '{script}'",
                root,
                0.2,
            )
            time.sleep(2.5)

            self.assertTrue(result.timed_out)
            self.assertFalse(marker.exists())

    async def test_missing_powershell_is_an_expected_boundary_error(self):
        runner = PowerShellCommandRunner(
            executable="missing-powershell-for-helperme-test.exe"
        )

        with self.assertRaisesRegex(FileNotFoundError, "未找到 PowerShell"):
            await runner.run("Write-Output ok", Path.cwd(), 10)


@unittest.skipUnless(POWERSHELL, "需要 Windows PowerShell")
class ExecuteCommandToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.directory.name)
        self.workspaces = WorkspaceSandboxes({
            "project": WorkspaceSandbox(self.workspace_root)
        })
        registry = ToolRegistry()
        registry.register(
            create_command_execution_spec(
                self.workspaces,
                PowerShellCommandRunner(),
            )
        )
        self.executor = ToolsExecutor(registry)

    def tearDown(self):
        self.directory.cleanup()

    async def execute(self, payload: dict) -> dict:
        return await self.executor.execute(
            "execute_command",
            json.dumps(payload, ensure_ascii=False),
        )

    async def test_nonzero_exit_is_a_completed_command(self):
        result = await self.execute({
            "root": "project",
            "command": 'Write-Error "failed"',
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "COMMAND_COMPLETED")
        self.assertEqual(result["data"]["exit_code"], 1)
        self.assertIn("failed", result["data"]["stderr"]["content"])

    async def test_workspace_effect_defaults_to_may_write(self):
        result = await self.execute({
            "root": "project",
            "command": "Write-Output ok",
        })

        self.assertEqual(result["data"]["workspace_effect"], "may_write")

    async def test_read_only_workspace_effect_is_returned(self):
        result = await self.execute({
            "root": "project",
            "command": "Write-Output ok",
            "workspace_effect": "read_only",
        })

        self.assertEqual(result["data"]["workspace_effect"], "read_only")

    async def test_runs_in_workspace_relative_cwd(self):
        child = self.workspace_root / "child"
        child.mkdir()

        result = await self.execute({
            "root": "project",
            "cwd": "child",
            "command": "(Get-Location).Path",
        })

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["cwd"], "child")
        self.assertEqual(
            Path(result["data"]["stdout"]["content"].strip()).resolve(),
            child.resolve(),
        )

    async def test_rejects_absolute_and_escaping_cwd(self):
        absolute = await self.execute({
            "root": "project",
            "cwd": str(self.workspace_root),
            "command": "Write-Output ok",
        })
        escaping = await self.execute({
            "root": "project",
            "cwd": "../outside",
            "command": "Write-Output ok",
        })

        self.assertEqual(absolute["code"], "ABSOLUTE_PATH_NOT_ALLOWED")
        self.assertEqual(escaping["code"], "PATH_OUTSIDE_WORKSPACE")

    async def test_command_may_use_an_absolute_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as outside_directory:
            outside_file = Path(outside_directory) / "outside.txt"
            outside_file.write_text("outside-fact", encoding="utf-8")
            literal_path = str(outside_file).replace("'", "''")

            result = await self.execute({
                "root": "project",
                "command": f"Get-Content -LiteralPath '{literal_path}' -Raw",
            })

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["data"]["stdout"]["content"].strip(),
            "outside-fact",
        )

    async def test_empty_command_is_rejected(self):
        result = await self.execute({"root": "project", "command": "  "})

        self.assertEqual(result["code"], "EMPTY_COMMAND")

    async def test_timeout_is_a_tool_failure_with_partial_output(self):
        result = await self.execute({
            "root": "project",
            "command": 'Write-Output "started"; Start-Sleep -Seconds 5',
            "timeout_seconds": 1,
        })

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "COMMAND_TIMEOUT")
        self.assertTrue(result["data"]["timed_out"])
        self.assertIn("started", result["data"]["stdout"]["content"])

    async def test_timeout_schema_limits_are_exposed_and_validated(self):
        schema = ExecuteCommandInput.model_json_schema()["properties"]
        self.assertEqual(schema["timeout_seconds"]["minimum"], 1)
        self.assertEqual(schema["timeout_seconds"]["maximum"], 300)
        with self.assertRaises(ValidationError):
            ExecuteCommandInput.model_validate({
                "root": "project",
                "command": "ok",
                "timeout_seconds": 301,
            })
        with self.assertRaises(ValidationError):
            ExecuteCommandInput.model_validate({
                "root": "project",
                "command": "ok",
                "workspace_effect": "unknown",
            })


if __name__ == "__main__":
    unittest.main()
