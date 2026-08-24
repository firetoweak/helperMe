from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, call, patch

from helperme.channels.cli.console import read_console_input


class ConsoleInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_continuously_collects_complete_lines(self):
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        session = AsyncMock()
        session.prompt_async.side_effect = (
            "第一个任务",
            "运行时打断",
            EOFError,
        )

        with patch("helperme.channels.cli.console.patch_stdout") as patched:
            await read_console_input(queue, session)

        patched.assert_called_once_with()
        self.assertEqual(
            session.prompt_async.await_args_list,
            [call("你："), call("你："), call("你：")],
        )
        self.assertEqual(await queue.get(), "第一个任务")
        self.assertEqual(await queue.get(), "运行时打断")
        self.assertIsNone(await queue.get())
