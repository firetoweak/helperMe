from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, call, patch

from helperme.channels.cli.console import _ContextMeter, read_console_input


class ConsoleInputTests(unittest.IsolatedAsyncioTestCase):
    def test_context_meter_tracks_only_the_selected_stream(self):
        meter = _ContextMeter()
        meter.select("stream-1", 200_000)

        meter.update("another-stream", 90_000, 200_000)
        self.assertEqual(meter.render(), "上下文 0/200k")

        meter.update("stream-1", 12_345, 200_000)
        self.assertEqual(meter.render(), "上下文 12.3k/200k")

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
            [
                call("你：", refresh_interval=0.25),
                call("你：", refresh_interval=0.25),
                call("你：", refresh_interval=0.25),
            ],
        )
        self.assertEqual(await queue.get(), "第一个任务")
        self.assertEqual(await queue.get(), "运行时打断")
        self.assertIsNone(await queue.get())
