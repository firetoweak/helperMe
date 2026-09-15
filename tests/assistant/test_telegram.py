from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from helperme.assistant.sessions import SessionView
from helperme.assistant.runner import SessionNotFoundError


def _telegram():
    from helperme.channels.telegram.assistant import (
        TelegramChannel,
        TelegramPairing,
        _open_chat_channel,
    )

    return TelegramChannel, TelegramPairing, _open_chat_channel


class TelegramChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_startup_resumes_session_for_same_bot_and_chat(self) -> None:
        sessions = AsyncMock()
        sessions.select.return_value = _session_view()
        sessions.accept_input.return_value = _session_view()
        bot = AsyncMock()

        _TelegramChannel, _TelegramPairing, open_chat_channel = _telegram()
        channel = await open_chat_channel(sessions, bot, 101, 7)

        sessions.select.assert_awaited_once_with(
            "telegram-bot-101-chat-7", "telegram-bot-101-chat-7"
        )
        sessions.create.assert_not_awaited()
        await channel.accept(10, _message(7, "新任务"))
        sessions.accept_input.assert_awaited_once_with(
            "telegram-bot-101-chat-7",
            "新任务",
            delivery_id="telegram-bot-101-update-10",
            source="telegram",
        )

    async def test_new_bot_creates_its_own_session(self) -> None:
        sessions = AsyncMock()
        sessions.select.side_effect = (
            SessionNotFoundError("missing"),
            _session_view(),
        )

        _TelegramChannel, _TelegramPairing, open_chat_channel = _telegram()
        await open_chat_channel(sessions, AsyncMock(), 202, 7)

        self.assertEqual(sessions.select.await_count, 2)
        sessions.create.assert_awaited_once_with("telegram-bot-202-chat-7")

    async def test_unpaired_start_reports_chat_id_without_touching_runtime(
        self,
    ) -> None:
        _TelegramChannel, TelegramPairing, _open_chat_channel = _telegram()
        bot = AsyncMock()
        pairing = TelegramPairing(bot)

        with patch("builtins.print"):
            await pairing.accept(_message(17, "/start"))

        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 17)
        self.assertIn("17", bot.send_message.await_args.kwargs["text"])

    async def test_unpaired_message_cannot_create_session(self) -> None:
        _TelegramChannel, TelegramPairing, _open_chat_channel = _telegram()
        bot = AsyncMock()
        pairing = TelegramPairing(bot)

        await pairing.accept(_message(17, "hello"))

        self.assertEqual(bot.method_calls, [])

    async def test_start_replies_without_touching_runtime(self) -> None:
        TelegramChannel, _TelegramPairing, _open_chat_channel = _telegram()
        bot = AsyncMock()
        sessions = AsyncMock()
        channel = TelegramChannel(sessions, bot, 7, "session-current", 101)

        await channel.accept(10, _message(7, "/start"))

        bot.send_message.assert_awaited_once_with(
            chat_id=7,
            text="HelperMe 已连接。直接发送任务即可。",
        )
        self.assertEqual(sessions.method_calls, [])

    async def test_message_is_persisted_and_scheduler_is_owned_by_sessions(
        self,
    ) -> None:
        TelegramChannel, _TelegramPairing, _open_chat_channel = _telegram()
        bot = AsyncMock()
        sessions = AsyncMock()
        sessions.accept_input.return_value = _session_view()
        channel = TelegramChannel(sessions, bot, 7, "session-current", 101)

        await channel.accept(11, _message(7, "帮我看看"))

        sessions.accept_input.assert_awaited_once_with(
            "session-current",
            "帮我看看",
            delivery_id="telegram-bot-101-update-11",
            source="telegram",
        )
        self.assertFalse(hasattr(channel, "drive_next"))

    async def test_each_message_is_an_ordered_user_event(self) -> None:
        TelegramChannel, _TelegramPairing, _open_chat_channel = _telegram()
        sessions = AsyncMock()
        sessions.accept_input.return_value = _session_view()
        channel = TelegramChannel(
            sessions,
            AsyncMock(),
            7,
            "session-current",
            101,
        )

        await channel.accept(11, _message(7, "先检查项目"))
        await channel.accept(12, _message(7, "停一下，先别执行"))

        self.assertEqual(sessions.accept_input.await_count, 2)
        self.assertEqual(
            sessions.accept_input.await_args_list[1].args,
            ("session-current", "停一下，先别执行"),
        )

    async def test_authorization_reply_resumes_session(self) -> None:
        TelegramChannel, _TelegramPairing, _open_chat_channel = _telegram()
        sessions = AsyncMock()
        sessions.accept_input.return_value = _session_view()
        channel = TelegramChannel(
            sessions,
            AsyncMock(),
            7,
            "session-current",
            101,
        )

        await channel.accept(12, _message(7, "yes"))

        sessions.accept_input.assert_awaited_once_with(
            "session-current",
            "yes",
            delivery_id="telegram-bot-101-update-12",
            source="telegram",
        )

    async def test_other_chat_is_ignored(self) -> None:
        TelegramChannel, _TelegramPairing, _open_chat_channel = _telegram()
        sessions = AsyncMock()
        bot = AsyncMock()
        channel = TelegramChannel(sessions, bot, 7, "session-current", 101)

        await channel.accept(13, _message(8, "hello"))

        self.assertEqual(sessions.method_calls, [])
        self.assertEqual(bot.method_calls, [])

    async def test_preview_edits_one_message_and_final_is_idempotent(self) -> None:
        TelegramChannel, _TelegramPairing, _open_chat_channel = _telegram()
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(message_id=31)
        channel = TelegramChannel(
            AsyncMock(), bot, 7, "session-current", 101
        )

        await channel.preview("session-current", "started", "output-1", None)
        await channel.preview("session-current", "delta", "output-1", "你")
        await channel.preview("session-current", "delta", "output-1", "好")
        await channel.deliver("session-current", "output-1", "你好")
        await channel.deliver("session-current", "output-1", "你好")

        bot.send_message.assert_awaited_once_with(chat_id=7, text="你")
        bot.edit_message_text.assert_awaited_once_with(
            chat_id=7,
            message_id=31,
            text="你好",
        )

    async def test_aborted_preview_is_marked_without_final_delivery(self) -> None:
        TelegramChannel, _TelegramPairing, _open_chat_channel = _telegram()
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(message_id=31)
        channel = TelegramChannel(
            AsyncMock(), bot, 7, "session-current", 101
        )

        await channel.preview("session-current", "started", "output-1", None)
        await channel.preview("session-current", "delta", "output-1", "partial")
        await channel.preview("session-current", "aborted", "output-1", None)

        bot.edit_message_text.assert_awaited_once_with(
            chat_id=7,
            message_id=31,
            text="partial\n\n[输出已中止]",
        )

    async def test_final_delivery_retries_transient_telegram_failure(self) -> None:
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import SendMessage

        TelegramChannel, _TelegramPairing, _open_chat_channel = _telegram()
        bot = AsyncMock()
        bot.send_message.side_effect = (
            TelegramNetworkError(
                SendMessage(chat_id=7, text="done"),
                "temporary failure",
            ),
            SimpleNamespace(message_id=31),
        )
        channel = TelegramChannel(
            AsyncMock(), bot, 7, "session-current", 101
        )

        with patch(
            "helperme.channels.telegram.assistant.asyncio.sleep",
            new=AsyncMock(),
        ):
            await channel.deliver("session-current", "output-1", "done")

        self.assertEqual(bot.send_message.await_count, 2)


def _message(chat_id: int, text: str):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), text=text)


def _session_view(**overrides) -> SessionView:
    values = {
            "status": "waiting",
            "waiting_for": ("user_message",),
            "pending_authorization_ids": (),
            "should_wake": False,
    }
    values.update(overrides)
    return SessionView(**values)
