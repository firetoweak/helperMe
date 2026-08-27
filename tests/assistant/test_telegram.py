from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from helperme.assistant.sessions import SessionView
from helperme.assistant.runner import SessionNotFoundError
from helperme.channels.telegram.assistant import (
    TelegramChannel,
    TelegramPairing,
    _open_chat_channel,
)


class TelegramChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_resumes_session_for_same_bot_and_chat(self) -> None:
        sessions = AsyncMock()
        sessions.view.return_value = _session_view()
        bot = AsyncMock()

        channel = await _open_chat_channel(sessions, bot, 101, 7)

        sessions.resume.assert_awaited_once_with(
            "telegram-bot-101-chat-7"
        )
        sessions.create.assert_not_awaited()
        await channel.accept(10, _message(7, "新任务"))
        sessions.receive_user_message.assert_awaited_once_with(
            "telegram-bot-101-chat-7",
            "新任务",
            delivery_id="telegram-bot-101-update-10",
            source="telegram",
        )

    async def test_new_bot_creates_its_own_session(self) -> None:
        sessions = AsyncMock()
        sessions.resume.side_effect = SessionNotFoundError("missing")

        await _open_chat_channel(sessions, AsyncMock(), 202, 7)

        sessions.resume.assert_awaited_once_with(
            "telegram-bot-202-chat-7"
        )
        sessions.create.assert_awaited_once_with(
            "telegram-bot-202-chat-7"
        )

    async def test_unpaired_start_reports_chat_id_without_touching_runtime(
        self,
    ) -> None:
        bot = AsyncMock()
        pairing = TelegramPairing(bot)

        with patch("builtins.print"):
            await pairing.accept(_message(17, "/start"))

        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 17)
        self.assertIn("17", bot.send_message.await_args.kwargs["text"])

    async def test_unpaired_message_cannot_create_session(self) -> None:
        bot = AsyncMock()
        pairing = TelegramPairing(bot)

        await pairing.accept(_message(17, "hello"))

        self.assertEqual(bot.method_calls, [])

    async def test_start_replies_without_touching_runtime(self) -> None:
        bot = AsyncMock()
        sessions = AsyncMock()
        channel = TelegramChannel(sessions, bot, 7, "session-current", 101)

        await channel.accept(10, _message(7, "/start"))

        bot.send_message.assert_awaited_once_with(
            chat_id=7,
            text="HelperMe 已连接。直接发送任务即可。",
        )
        self.assertEqual(sessions.method_calls, [])

    async def test_message_is_persisted_before_worker_drives(self) -> None:
        bot = AsyncMock()
        sessions = AsyncMock()
        sessions.view.return_value = _session_view()
        channel = TelegramChannel(sessions, bot, 7, "session-current", 101)

        await channel.accept(11, _message(7, "帮我看看"))

        sessions.receive_user_message.assert_awaited_once_with(
            "session-current",
            "帮我看看",
            delivery_id="telegram-bot-101-update-11",
            source="telegram",
        )
        sessions.drive.assert_not_awaited()

        await channel.drive_next()
        sessions.drive.assert_awaited_once_with("session-current")
        sessions.resume.assert_not_awaited()

    async def test_message_during_drive_becomes_interrupt(self) -> None:
        sessions = AsyncMock()
        sessions.view.return_value = _session_view()
        drive_started = asyncio.Event()
        release_drive = asyncio.Event()

        async def drive(_session_id: str) -> None:
            drive_started.set()
            await release_drive.wait()

        sessions.drive.side_effect = drive
        channel = TelegramChannel(
            sessions,
            AsyncMock(),
            7,
            "session-current",
            101,
        )

        await channel.accept(11, _message(7, "先检查项目"))
        running = asyncio.create_task(channel.drive_next())
        await drive_started.wait()
        await channel.accept(12, _message(7, "停一下，先别执行"))

        sessions.receive_interrupt.assert_awaited_once_with(
            "session-current",
            "停一下，先别执行",
            delivery_id="telegram-bot-101-update-12",
            source="telegram",
        )
        release_drive.set()
        await running

    async def test_authorization_reply_resumes_session(self) -> None:
        sessions = AsyncMock()
        sessions.view.return_value = _session_view(
            pending_authorization_ids=("command-1",)
        )
        channel = TelegramChannel(
            sessions,
            AsyncMock(),
            7,
            "session-current",
            101,
        )

        await channel.accept(12, _message(7, "yes"))

        sessions.resolve_authorizations.assert_awaited_once_with(
            "session-current",
            approved=True,
        )
        await channel.drive_next()
        sessions.drive.assert_awaited_once_with("session-current")

    async def test_other_chat_is_ignored(self) -> None:
        sessions = AsyncMock()
        bot = AsyncMock()
        channel = TelegramChannel(sessions, bot, 7, "session-current", 101)

        await channel.accept(13, _message(8, "hello"))

        self.assertEqual(sessions.method_calls, [])
        self.assertEqual(bot.method_calls, [])


def _message(chat_id: int, text: str):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), text=text)


def _session_view(**overrides) -> SessionView:
    values = {
        "status": "waiting",
        "waiting_for": ("user_message",),
        "pending_authorization_ids": (),
        "terminal": False,
        "should_drive": False,
    }
    values.update(overrides)
    return SessionView(**values)


if __name__ == "__main__":
    unittest.main()
