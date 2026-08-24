from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from helperme.channels.cli.console import _poll_line


@unittest.skipUnless(sys.platform == "win32", "Windows console input only")
class WindowsConsoleInputTests(unittest.TestCase):
    def test_partial_line_survives_poll_timeout(self):
        buffer: list[str] = []

        with (
            patch(
                "helperme.channels.cli.console.time.monotonic",
                side_effect=(0.0, 0.0, 2.0, 0.0, 0.0),
            ),
            patch("msvcrt.kbhit", side_effect=(True, True)),
            patch("msvcrt.getwch", side_effect=("慢", "\r")),
            patch("builtins.print"),
        ):
            self.assertIsNone(_poll_line(1.0, buffer))
            self.assertEqual(buffer, ["慢"])
            self.assertEqual(_poll_line(1.0, buffer), "慢")

        self.assertEqual(buffer, [])
