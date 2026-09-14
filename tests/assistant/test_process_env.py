from __future__ import annotations

import os
import shutil
import unittest
from unittest.mock import patch

from helperme.assistant.host.process_env import host_process_environment


class HostProcessEnvironmentTest(unittest.TestCase):
    def test_keeps_current_path_prefix_and_appends_missing_session_dirs(self):
        session = {
            "PATH": os.pathsep.join(
                (
                    r"C:\Windows\System32\WindowsPowerShell\v1.0",
                    r"C:\Windows\system32",
                )
            ),
            "PATHEXT": ".COM;.EXE",
        }
        with patch(
            "helperme.assistant.host.process_env.user_session_environment",
            return_value=session,
        ):
            built = host_process_environment(
                {
                    "PATH": os.pathsep.join((r"C:\conda\env", r"C:\Windows\system32")),
                    "LITELLM_X": "1",
                }
            )

        parts = built["PATH"].split(os.pathsep)
        self.assertEqual(built["LITELLM_X"], "1")
        self.assertEqual(parts[0], r"C:\conda\env")
        self.assertIn(r"C:\Windows\System32\WindowsPowerShell\v1.0", parts)

    def test_fills_missing_identity_variable_from_user_session(self):
        with patch(
            "helperme.assistant.host.process_env.user_session_environment",
            return_value={"PATH": r"C:\Windows", "PATHEXT": ".EXE"},
        ):
            built = host_process_environment({"PATH": r"C:\Windows"})

        self.assertEqual(built["PATHEXT"], ".EXE")

    def test_leaves_environment_unchanged_when_no_session_block(self):
        current = {"PATH": "/usr/bin", "FOO": "bar"}
        with patch(
            "helperme.assistant.host.process_env.user_session_environment",
            return_value=None,
        ):
            built = host_process_environment(current)

        self.assertEqual(built, current)

    @unittest.skipUnless(os.name == "nt", "需要 Windows 用户会话环境")
    def test_stripped_path_can_discover_powershell_from_user_session(self):
        built = host_process_environment(
            {
                "PATH": r"C:\Windows\system32",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
            }
        )

        self.assertEqual(built["LITELLM_LOCAL_MODEL_COST_MAP"], "True")
        self.assertIsNotNone(shutil.which("powershell.exe", path=built["PATH"]))
