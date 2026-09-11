from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, Update

from helperme.assistant.runner import SessionNotFoundError
from helperme.assistant.ipc import WorkerFailed
from helperme.assistant.sessions import AssistantSessions
from helperme.config import InitialConfigCreated, load_app_config


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
        sessions: AssistantSessions,
        bot: Bot,
        chat_id: int,
        session_id: str,
        bot_id: int,
    ) -> None:
        self._sessions = sessions
        self._bot = bot
        self._chat_id = chat_id
        self._session_id = session_id
        self._delivery_prefix = f"telegram-bot-{bot_id}-update-"
        self.owner = f"telegram-bot-{bot_id}-chat-{chat_id}"

    async def send(self, text: str) -> None:
        await self._bot.send_message(chat_id=self._chat_id, text=text)

    async def accept(self, update_id: int, message: Message) -> None:
        if message.chat.id != self._chat_id or message.text is None:
            return
        if message.text.strip() == "/start":
            await self.send("HelperMe 已连接。直接发送任务即可。")
            return

        view = await self._sessions.accept_input(
            self._session_id,
            message.text,
            delivery_id=f"{self._delivery_prefix}{update_id}",
            source="telegram",
        )
        if view.control_message is not None:
            await self.send(view.control_message)
        elif view.terminal:
            await self.send("当前 Session 已结束。")


async def _open_chat_channel(
    sessions: AssistantSessions,
    bot: Bot,
    bot_id: int,
    chat_id: int,
) -> TelegramChannel:
    session_id = f"telegram-bot-{bot_id}-chat-{chat_id}"
    owner = f"telegram-bot-{bot_id}-chat-{chat_id}"
    try:
        await sessions.select(owner, session_id)
    except SessionNotFoundError:
        await sessions.create(session_id)
        await sessions.select(owner, session_id)
    return TelegramChannel(sessions, bot, chat_id, session_id, bot_id)


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

            print("Telegram 配对模式已启动；请向机器人发送 /start 获取 chat_id。")
            await dispatcher.start_polling(
                bot,
                allowed_updates=["message"],
                handle_as_tasks=False,
                close_bot_session=False,
            )
            return

        channel: TelegramChannel | None = None

        async def send(_session_id: str, text: str) -> None:
            assert channel is not None
            await channel.send(text)

        async with bootstrap_assistant(send, app_config=app_config) as app:
            channel = await _open_chat_channel(
                app.sessions,
                bot,
                bot.id,
                chat_id,
            )
            dispatcher = Dispatcher()

            @dispatcher.message(F.text)
            async def receive_text(
                message: Message,
                event_update: Update,
            ) -> None:
                try:
                    await channel.accept(event_update.update_id, message)
                except WorkerFailed as error:
                    await channel.send(f"Session 运行失败：{error}")

            polling = asyncio.create_task(
                dispatcher.start_polling(
                    bot,
                    allowed_updates=["message"],
                    handle_as_tasks=False,
                    close_bot_session=False,
                ),
                name="telegram-polling",
            )
            failure = asyncio.create_task(
                app.sessions.wait_failure(),
                name="assistant-failure",
            )
            try:
                while True:
                    done, _ = await asyncio.wait(
                        (polling, failure), return_when=asyncio.FIRST_COMPLETED
                    )
                    if polling in done:
                        await polling
                        break
                    await channel.send(f"Session 运行失败：{failure.result()}")
                    failure = asyncio.create_task(app.sessions.wait_failure())
            finally:
                for task in (polling, failure):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(polling, failure, return_exceptions=True)
                await app.sessions.release(channel.owner)


def main() -> None:
    try:
        asyncio.run(run_telegram_assistant())
    except InitialConfigCreated as exc:
        print(exc)
    except KeyboardInterrupt:
        print("\nTelegram Assistant 已退出。")
