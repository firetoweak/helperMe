from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from helperme.assistant.runner import StreamNotFoundError
from helperme.assistant.streams import StreamView
from helperme.channels.telegram.assistant import TelegramChannel


class TelegramChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_replies_without_touching_runtime(self) -> None:
        bot = AsyncMock()
        streams = AsyncMock()
        channel = TelegramChannel(streams, bot, 7)

        await channel.accept(10, _message(7, "/start"))

        bot.send_message.assert_awaited_once_with(
            chat_id=7,
            text="HelperMe 已连接。直接发送任务即可。",
        )
        self.assertEqual(streams.method_calls, [])

    async def test_message_is_persisted_before_worker_drives(self) -> None:
        bot = AsyncMock()
        streams = AsyncMock()
        streams.resume.side_effect = StreamNotFoundError("missing")
        streams.create.return_value = _stream_view()
        channel = TelegramChannel(streams, bot, 7)

        await channel.accept(11, _message(7, "帮我看看"))

        streams.receive_user_message.assert_awaited_once_with(
            "telegram-chat-v2-7",
            "帮我看看",
            delivery_id="telegram-update-11",
            source="telegram",
        )
        streams.drive.assert_not_awaited()

        await channel.drive_next()
        streams.drive.assert_awaited_once_with("telegram-chat-v2-7")

    async def test_authorization_reply_resumes_stream(self) -> None:
        streams = AsyncMock()
        streams.resume.return_value = _stream_view(
            pending_authorization_ids=("command-1",)
        )
        channel = TelegramChannel(streams, AsyncMock(), 7)

        await channel.accept(12, _message(7, "yes"))

        streams.resolve_authorizations.assert_awaited_once_with(
            "telegram-chat-v2-7",
            approved=True,
        )
        await channel.drive_next()
        streams.drive.assert_awaited_once_with("telegram-chat-v2-7")

    async def test_other_chat_is_ignored(self) -> None:
        streams = AsyncMock()
        bot = AsyncMock()
        channel = TelegramChannel(streams, bot, 7)

        await channel.accept(13, _message(8, "hello"))

        self.assertEqual(streams.method_calls, [])
        self.assertEqual(bot.method_calls, [])


def _message(chat_id: int, text: str):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), text=text)


def _stream_view(**overrides) -> StreamView:
    values = {
        "status": "waiting",
        "waiting_for": ("user_message",),
        "pending_authorization_ids": (),
        "terminal": False,
        "should_drive": False,
    }
    values.update(overrides)
    return StreamView(**values)


if __name__ == "__main__":
    unittest.main()
