from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, call, patch

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import HSplit, Window
from prompt_toolkit.output import DummyOutput

from helperme.channels.cli.console import (
    _BottomAnchoredPromptSession,
    _ContextMeter,
    read_console_input,
)


class ConsoleInputTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_is_anchored_above_the_bottom_toolbar(self):
        with create_pipe_input() as console_input:
            session = _BottomAnchoredPromptSession(
                bottom_toolbar=lambda: "上下文 0/200k",
                input=console_input,
                output=DummyOutput(),
            )

            root = session.layout.container
            self.assertIsInstance(root, HSplit)
            self.assertIsInstance(root.children[0], Window)
            prompt = root.children[1]
            self.assertIsInstance(prompt, HSplit)
            self.assertEqual(prompt.preferred_height(80, 24).min, 2)
            self.assertEqual(prompt.preferred_height(80, 24).max, 2)

    def test_context_meter_tracks_only_the_selected_session(self):
        meter = _ContextMeter()
        meter.select("session-1", 200_000)

        meter.update("another-session", 90_000, 200_000)
        self.assertEqual(meter.render(), "上下文 0/200k")

        meter.update("session-1", 12_345, 200_000)
        self.assertEqual(meter.render(), "上下文 12.3k/200k")

        meter.update_subagent_activity("another-session", True)
        self.assertEqual(meter.render(), "上下文 12.3k/200k")

        meter.update_subagent_activity("session-1", True)
        self.assertEqual(
            meter.render(),
            "上下文 12.3k/200k  ·  子 Agent 工作中",
        )

        meter.update_subagent_activity("session-1", False)
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
