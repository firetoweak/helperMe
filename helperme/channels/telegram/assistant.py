from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Update

from helperme.assistant.runner import MODEL_DECISION_ERRORS, StreamNotFoundError
from helperme.assistant.streams import AssistantStreams
from helperme.config import InitialConfigCreated, load_app_config


_STREAM_PREFIX = "telegram-chat-v2-"
_YES = {"yes", "y"}
_NO = {"no", "n"}


class TelegramPairing:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def accept(self, message: Message) -> None:
        if message.text is None or message.text.strip() != "/start":
            return
        chat_id = message.chat.id
        print(
            f"Telegram 配对请求：chat_id={chat_id}；"
            "请写入 ~/.helperme/config.json 后重启。"
        )
        await self._bot.send_message(
            chat_id=chat_id,
            text=(
                f"当前 chat_id：{chat_id}\n"
                "请将它填入 config.json 的 "
                "channels.telegram.allowed_chat_id，然后重启 HelperMe。"
            ),
        )


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

    app_config = load_app_config()
    telegram = app_config.channels.telegram
    if telegram is None:
        raise RuntimeError("请在 config.json 中配置 channels.telegram")
    chat_id = telegram.allowed_chat_id
    async with Bot(token=telegram.bot_token) as bot:
        if chat_id is None:
            pairing = TelegramPairing(bot)
            dispatcher = Dispatcher()

            @dispatcher.message(F.text)
            async def receive_pairing(message: Message) -> None:
                await pairing.accept(message)

            print(
                "Telegram 配对模式已启动；"
                "请向机器人发送 /start 获取 chat_id。"
            )
            await dispatcher.start_polling(
                bot,
                allowed_updates=["message"],
                handle_as_tasks=False,
                close_bot_session=False,
            )
            return

        channel: TelegramChannel | None = None

        async def send(text: str) -> None:
            assert channel is not None
            await channel.send(text)

        async with bootstrap_assistant(send, app_config=app_config) as app:
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
            print(
                f"Telegram Assistant 已启动，chat_id={chat_id}；"
                "按 Ctrl+C 退出。"
            )
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
    except InitialConfigCreated as exc:
        print(exc)
    except KeyboardInterrupt:
        print("\nTelegram Assistant 已退出。")
