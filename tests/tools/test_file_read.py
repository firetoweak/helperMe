from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from helperme.sandbox.api import EnvironmentBinding, ExecutionAttachment
from helperme.sandbox.workspace import (
    FilesystemPermission,
    PermissionBinding,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from helperme.tools.builtin.file_read import GlobInput, create_file_read_specs


def _binding(root: Path) -> EnvironmentBinding:
    view = WorkspaceViewSnapshot((
        RootBinding("project", WorkspaceScope.TASK, root),
    ))
    return EnvironmentBinding(
        environment_id="local-test",
        workspace_view=view,
        permission_binding=PermissionBinding((
            ("project", FilesystemPermission.READ_WRITE),
        )),
        cwd=root,
        shell_name="powershell",
        shell_path="pwsh.exe",
        execution_attachment=ExecutionAttachment("local-test", object()),
    )


class GlobSchedulingTest(unittest.IsolatedAsyncioTestCase):
    async def test_scan_runs_outside_event_loop_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            binding = _binding(Path(directory))
            glob = create_file_read_specs(binding)[0].handler
            event_loop_thread = threading.get_ident()
            scan_thread = None

            def scan(*_args):
                nonlocal scan_thread
                scan_thread = threading.get_ident()
                return {"ok": True, "code": "GLOB_COMPLETED"}

            with patch(
                "helperme.tools.builtin.file_read._scan_glob",
                side_effect=scan,
            ):
                result = await glob(GlobInput(pattern="*"))

        self.assertEqual(result, {"ok": True, "code": "GLOB_COMPLETED"})
        self.assertIsNotNone(scan_thread)
        self.assertNotEqual(scan_thread, event_loop_thread)


if __name__ == "__main__":
    unittest.main()
