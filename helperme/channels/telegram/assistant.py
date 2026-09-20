from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import Message, Update

from helperme.assistant.conversations import SessionSummary, recent_workspace_id
from helperme.assistant.runner import SessionNotFoundError
from helperme.assistant.host.ipc import WorkerFailed
from helperme.assistant.sessions import AssistantSessions
from helperme.config import InitialConfigCreated, load_app_config
from helperme.sandbox.registry import WorkspaceRecord, WorkspaceRegistry


_PREVIEW_EDIT_INTERVAL = 0.5


@dataclass(slots=True)
class _TelegramPreview:
    content: str = ""
    published: str = ""
    message_id: int | None = None
    flush: asyncio.Task | None = None


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
        self._previews: dict[tuple[str, str], _TelegramPreview] = {}
        self._delivered: dict[tuple[str, str], str] = {}
        self.owner = f"telegram-bot-{bot_id}-chat-{chat_id}"

    async def send(self, text: str) -> None:
        await self._bot.send_message(chat_id=self._chat_id, text=text)

    async def deliver(self, session_id: str, output_id: str, text: str) -> None:
        key = (session_id, output_id)
        if key in self._delivered:
            if self._delivered[key] != text:
                raise RuntimeError("output_id was delivered with different text")
            return
        preview = self._previews.get(key)
        if preview is None:
            await self._retry(lambda: self.send(text))
        else:
            if preview.content.strip() != text:
                raise RuntimeError("committed output differs from its preview")
            await self._stop_flush(preview)
            if preview.message_id is None:
                await self._retry(lambda: self.send(text))
            elif preview.published != text:
                await self._retry(lambda: self._edit(preview.message_id, text))
            del self._previews[key]
        self._delivered[key] = text

    async def preview(
        self,
        session_id: str,
        phase: str,
        output_id: str,
        text: str | None,
    ) -> None:
        key = (session_id, output_id)
        if phase == "started":
            self._previews[key] = _TelegramPreview()
            return
        preview = self._previews[key]
        if phase == "delta":
            preview.content += text
            if preview.message_id is None:
                try:
                    message = await self._bot.send_message(
                        chat_id=self._chat_id,
                        text=preview.content,
                    )
                except (TelegramNetworkError, TelegramRetryAfter, TelegramServerError):
                    return
                preview.message_id = message.message_id
                preview.published = preview.content
            elif preview.flush is None:
                preview.flush = asyncio.create_task(self._flush_later(key))
            return
        if phase == "aborted":
            await self._stop_flush(preview)
            del self._previews[key]
            if preview.message_id is not None:
                try:
                    await self._edit(
                        preview.message_id,
                        preview.published + "\n\n[输出已中止]",
                    )
                except (
                    TelegramNetworkError,
                    TelegramRetryAfter,
                    TelegramServerError,
                ):
                    pass
            return
        raise ValueError(f"unknown preview phase: {phase}")

    async def _flush_later(self, key: tuple[str, str]) -> None:
        preview = self._previews[key]
        try:
            await asyncio.sleep(_PREVIEW_EDIT_INTERVAL)
            await self._edit(preview.message_id, preview.content)
            preview.published = preview.content
        except (TelegramNetworkError, TelegramRetryAfter, TelegramServerError):
            pass
        finally:
            preview.flush = None

    async def _stop_flush(self, preview: _TelegramPreview) -> None:
        if preview.flush is None:
            return
        preview.flush.cancel()
        await asyncio.gather(preview.flush, return_exceptions=True)
        preview.flush = None

    async def _edit(self, message_id: int, text: str) -> None:
        await self._bot.edit_message_text(
            chat_id=self._chat_id,
            message_id=message_id,
            text=text,
        )

    async def _retry(self, operation) -> None:
        while True:
            try:
                await operation()
                return
            except TelegramRetryAfter as error:
                await asyncio.sleep(error.retry_after)
            except (TelegramNetworkError, TelegramServerError):
                await asyncio.sleep(1)

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
        if view.control_approval is not None:
            await self.send(
                f"{view.control_approval.summary}\n"
                f"风险：{view.control_approval.risk}\n"
                "输入 yes 确认，no 取消。"
            )
        elif view.control_message is not None:
            await self.send(view.control_message)


class TelegramWorkspaceRequired(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Telegram 没有可回退的工作区，请加上 --workspace <path>")


def resolve_telegram_workspace(
    workspaces: WorkspaceRegistry,
    summaries: tuple[SessionSummary, ...],
    workspace_path: Path | None,
) -> WorkspaceRecord:
    if workspace_path is not None:
        return workspaces.register_path(workspace_path)
    workspace_id = recent_workspace_id(summaries)
    if workspace_id is None:
        raise TelegramWorkspaceRequired()
    return workspaces.get(workspace_id)


async def _open_chat_channel(
    sessions: AssistantSessions,
    bot: Bot,
    bot_id: int,
    chat_id: int,
    workspace_id: str,
) -> TelegramChannel:
    session_id = f"telegram-bot-{bot_id}-chat-{chat_id}"
    owner = f"telegram-bot-{bot_id}-chat-{chat_id}"
    try:
        await sessions.select(owner, session_id)
    except SessionNotFoundError:
        await sessions.create(session_id, workspace_id)
        await sessions.select(owner, session_id)
    return TelegramChannel(sessions, bot, chat_id, session_id, bot_id)


async def run_telegram_assistant(workspace_path: Path | None = None) -> None:
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

        async def send(session_id: str, output_id: str, text: str) -> None:
            assert channel is not None
            await channel.deliver(session_id, output_id, text)

        async def preview(
            session_id: str,
            phase: str,
            output_id: str,
            text: str | None,
        ) -> None:
            assert channel is not None
            await channel.preview(session_id, phase, output_id, text)

        async def session_failed(_session_id: str, message: str) -> None:
            assert channel is not None
            await channel.send(message)

        async with bootstrap_assistant(
            send,
            app_config=app_config,
            workspace_path=None,
            preview_sink=preview,
            session_failed_sink=session_failed,
        ) as app:
            workspace = resolve_telegram_workspace(
                app.workspaces,
                await app.queries.list_sessions(),
                workspace_path,
            )
            channel = await _open_chat_channel(
                app.sessions,
                bot,
                bot.id,
                chat_id,
                workspace.workspace_id,
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="工作区路径；缺省回退到最近一次聊天的工作区",
    )
    options = parser.parse_args(argv)
    try:
        asyncio.run(run_telegram_assistant(workspace_path=options.workspace))
    except InitialConfigCreated as exc:
        print(exc)
    except TelegramWorkspaceRequired as exc:
        print(exc)
    except KeyboardInterrupt:
        print("\nTelegram Assistant 已退出。")
