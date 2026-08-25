from __future__ import annotations

import asyncio
import os

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Update

from helperme.assistant.runner import MODEL_DECISION_ERRORS, StreamNotFoundError
from helperme.assistant.streams import AssistantStreams


TOKEN_ENV = "HELPER_TELEGRAM_BOT_TOKEN"
ALLOWED_CHAT_ENV = "HELPER_TELEGRAM_ALLOWED_CHAT_ID"
_STREAM_PREFIX = "telegram-chat-v2-"
_YES = {"yes", "y"}
_NO = {"no", "n"}


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"请设置环境变量 {name}")
    return value


def _allowed_chat_id() -> int:
    try:
        return int(_required_env(ALLOWED_CHAT_ENV))
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {ALLOWED_CHAT_ENV} 必须是整数") from exc


class TelegramChannel:
    def __init__(
        self,
        streams: AssistantStreams,
        bot: Bot,
        chat_id: int,
    ) -> None:
        self._streams = streams
        self._bot = bot
        self._chat_id = chat_id
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, text: str) -> None:
        await self._bot.send_message(chat_id=self._chat_id, text=text)

    async def accept(self, update_id: int, message: Message) -> None:
        if message.chat.id != self._chat_id or message.text is None:
            return
        if message.text.strip() == "/start":
            await self.send("HelperMe 已连接。直接发送任务即可。")
            return

        stream_id = f"{_STREAM_PREFIX}{self._chat_id}"
        try:
            view = await self._streams.resume(stream_id)
        except StreamNotFoundError:
            view = await self._streams.create(stream_id)

        answer = message.text.strip().lower()
        if view.pending_authorization_ids and answer in _YES | _NO:
            await self._streams.resolve_authorizations(
                stream_id,
                approved=answer in _YES,
            )
        else:
            await self._streams.receive_user_message(
                stream_id,
                message.text,
                delivery_id=f"telegram-update-{update_id}",
                source="telegram",
            )
        await self._queue.put(stream_id)

    async def run_worker(self) -> None:
        while True:
            await self.drive_next()

    async def drive_next(self) -> None:
        stream_id = await self._queue.get()
        try:
            await self._streams.drive(stream_id)
        except MODEL_DECISION_ERRORS as exc:
            await self.send(f"模型调用失败：{exc}")
        finally:
            self._queue.task_done()


async def run_telegram_assistant() -> None:
    from helperme.bootstrap import bootstrap_assistant

    chat_id = _allowed_chat_id()
    async with Bot(token=_required_env(TOKEN_ENV)) as bot:
        channel: TelegramChannel | None = None

        async def send(text: str) -> None:
            assert channel is not None
            await channel.send(text)

        async with bootstrap_assistant(send) as app:
            channel = TelegramChannel(app.streams, bot, chat_id)
            dispatcher = Dispatcher()

            @dispatcher.message(F.text)
            async def receive_text(
                message: Message,
                event_update: Update,
            ) -> None:
                await channel.accept(event_update.update_id, message)

            worker = asyncio.create_task(channel.run_worker())
            polling = asyncio.create_task(dispatcher.start_polling(
                bot,
                allowed_updates=["message"],
                handle_as_tasks=False,
                close_bot_session=False,
            ))
            print(f"Telegram Assistant 已启动，chat_id={chat_id}；按 Ctrl+C 退出。")
            try:
                done, _ = await asyncio.wait(
                    (worker, polling),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                await next(iter(done))
            finally:
                for task in (worker, polling):
                    task.cancel()
                await asyncio.gather(worker, polling, return_exceptions=True)


def main() -> None:
    try:
        asyncio.run(run_telegram_assistant())
    except KeyboardInterrupt:
        print("\nTelegram Assistant 已退出。")
