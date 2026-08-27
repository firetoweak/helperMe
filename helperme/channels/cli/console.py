from __future__ import annotations

import asyncio
from uuid import uuid4

from prompt_toolkit import PromptSession
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout

from helperme.assistant.sessions import (
    AssistantSessions,
    SessionNotFoundError,
    SessionView,
)
from helperme.assistant.runner import MODEL_DECISION_ERRORS
from helperme.assistant.toolsets import ToolsetLoadError
from helperme.bootstrap import bootstrap_assistant
from helperme.mcp.console import McpCommandError, McpConsoleAdapter
from helperme.mcp.errors import McpInputError
from helperme.skills.console import SkillCommandError, SkillConsoleAdapter
from helperme.skills.errors import SkillInputError


_TURN_RULE = "─" * 72


class _ConsoleInputClosed(Exception):
    pass


class _BottomAnchoredPromptSession(PromptSession[str]):
    def _create_layout(self) -> Layout:
        prompt_layout = super()._create_layout()
        prompt = HSplit(
            [prompt_layout.container],
            height=Dimension.exact(2),
        )
        return Layout(
            HSplit([Window(), prompt]),
            focused_element=prompt_layout.current_control,
        )


def _compact_tokens(tokens: int) -> str:
    if tokens < 1_000:
        return str(tokens)
    if tokens % 1_000 == 0:
        return f"{tokens // 1_000}k"
    return f"{tokens / 1_000:.1f}k"


class _ContextMeter:
    def __init__(self) -> None:
        self._session_id = ""
        self._used = 0
        self._limit = 0

    def select(self, session_id: str, limit: int) -> None:
        self._session_id = session_id
        self._used = 0
        self._limit = limit

    def update(self, session_id: str, used: int, limit: int) -> None:
        if session_id != self._session_id:
            return
        self._used = used
        self._limit = limit

    def render(self) -> str:
        return (
            f"上下文 {_compact_tokens(self._used)}/"
            f"{_compact_tokens(self._limit)}"
        )


async def _running_input(
    drive: asyncio.Task[SessionView],
    input_queue: asyncio.Queue[str | None],
) -> str | None:
    incoming = asyncio.create_task(input_queue.get())
    try:
        done, _ = await asyncio.wait(
            {drive, incoming},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if incoming in done:
            return incoming.result()
        return None
    finally:
        if not incoming.done():
            incoming.cancel()
            try:
                await incoming
            except asyncio.CancelledError:
                pass


async def drive_with_console_interrupts(
    sessions: AssistantSessions,
    session_id: str,
    input_queue: asyncio.Queue[str | None],
) -> SessionView:
    """Drive one Session while concurrent CLI text becomes a durable interrupt."""

    print("\n● 运行中", flush=True)
    drive = asyncio.create_task(sessions.drive(session_id))
    try:
        while not drive.done():
            text = await _running_input(
                drive,
                input_queue,
            )
            if drive.done() and text is None:
                break
            if text is None:
                if drive.done():
                    break
                raise _ConsoleInputClosed
            if not text:
                continue
            await sessions.receive_interrupt(
                session_id,
                text,
                delivery_id=f"interrupt-{uuid4().hex}",
            )
        return await drive
    finally:
        if not drive.done():
            drive.cancel()
            try:
                await drive
            except asyncio.CancelledError:
                pass
        print("\n○ 空闲", flush=True)


async def read_console_input(
    queue: asyncio.Queue[str | None],
    session: PromptSession[str],
) -> None:
    with patch_stdout():
        while True:
            try:
                text = await session.prompt_async(
                    "你：",
                    refresh_interval=0.25,
                )
            except (EOFError, KeyboardInterrupt):
                await queue.put(None)
                return
            await queue.put(text.strip())


def _print_runtime_status(view: SessionView) -> None:
    if view.control_message is not None:
        print(f"控制面：\n{view.control_message}")
    elif view.control_approval is not None:
        print(
            "控制面待确认：\n"
            f"{view.control_approval.summary}\n"
            f"风险：{view.control_approval.risk}\n"
            "输入 yes 确认，no 取消。"
        )
    print(f"Runtime 状态：{view.status}")
    if view.waiting_for:
        print("等待：" + ", ".join(view.waiting_for))
    if view.pending_authorization_ids:
        print(
            "有命令等待授权。yes / no 写入授权事实；"
            "其他话会写成 UserMessage，由下一步模型判断。"
        )


async def run_runtime_console() -> None:
    def sink(text: str) -> None:
        print(f"\n助手：{text}")

    context_meter = _ContextMeter()
    session: PromptSession[str] = _BottomAnchoredPromptSession(
        bottom_toolbar=context_meter.render,
    )
    async with bootstrap_assistant(
        sink,
        context_usage_sink=context_meter.update,
    ) as app:
        config = app.config
        sessions = app.sessions
        mcp_console = McpConsoleAdapter(app.mcp_service)
        skill_console = SkillConsoleAdapter(app.skill_service)
        session_id = f"session-{uuid4().hex}"
        await sessions.create(session_id)
        context_meter.select(session_id, config.model_context_limit)
        input_queue: asyncio.Queue[str | None] = asyncio.Queue()
        access = "整台电脑" if config.full_access else "配置的 Workspace"
        print(f"HelperMe 已启动。model={config.model_name}")
        print(f"工作区：{access}")
        print(f"当前对话：{session_id}")
        print("/new 新对话    /resume <id> 恢复")
        print("/mcp  /skill  管理外部能力")
        print("直接输入任务。运行中再输入会打断当前任务。")
        print("Ctrl+C 或 Ctrl+D 退出。")

        reader = asyncio.create_task(read_console_input(input_queue, session))
        try:
            separate_turns = False
            while True:
                if separate_turns:
                    print(f"\n{_TURN_RULE}", flush=True)
                    separate_turns = False
                user_message = await input_queue.get()
                if user_message is None:
                    print("\n已退出。")
                    return
                if not user_message:
                    continue
                separate_turns = True
                if user_message == "/new":
                    session_id = f"session-{uuid4().hex}"
                    await sessions.create(session_id)
                    context_meter.select(session_id, config.model_context_limit)
                    print(f"\n新 Session 已创建：{session_id}")
                    continue
                if user_message == "/resume" or user_message.startswith(
                    "/resume "
                ):
                    parts = user_message.split(maxsplit=1)
                    if len(parts) != 2 or not parts[1].strip():
                        print("\n用法：/resume <session_id>")
                        continue
                    target_session_id = parts[1].strip()
                    try:
                        view = await sessions.resume(target_session_id)
                    except SessionNotFoundError:
                        print(f"\nSession 不存在：{target_session_id}")
                        continue
                    except ToolsetLoadError as exc:
                        print(
                            f"\nSession 恢复失败：{exc.code}: {exc.message}"
                        )
                        continue
                    session_id = target_session_id
                    context_meter.select(session_id, config.model_context_limit)
                    print(f"\n已恢复 Session：{session_id}")
                    if view.should_drive:
                        try:
                            view = await drive_with_console_interrupts(
                                sessions,
                                session_id,
                                input_queue,
                            )
                        except _ConsoleInputClosed:
                            print("\n已退出。")
                            return
                        except MODEL_DECISION_ERRORS as exc:
                            print(f"\n模型调用失败：{exc}")
                            continue
                    _print_runtime_status(view)
                    continue
                try:
                    mcp_reply = await mcp_console.execute_if_handled(user_message)
                except (McpCommandError, McpInputError) as exc:
                    print(f"\nMCP：{exc}")
                    continue
                if mcp_reply is not None:
                    print(f"\nMCP：\n{mcp_reply}")
                    continue
                try:
                    skill_reply = await skill_console.execute_if_handled(
                        user_message,
                    )
                except (SkillCommandError, SkillInputError) as exc:
                    print(f"\nSkill：{exc}")
                    continue
                if skill_reply is not None:
                    print(f"\nSkill：\n{skill_reply}")
                    continue
                view = await sessions.view(session_id)
                if (
                    view.control_approval is not None
                    and user_message.lower() in {"yes", "y", "no", "n"}
                ):
                    message = await sessions.resolve_control(
                        session_id,
                        approved=user_message.lower() in {"yes", "y"},
                    )
                    print(f"\n控制面：{message}")
                    _print_runtime_status(await sessions.view(session_id))
                    continue
                if (
                    view.pending_authorization_ids
                    and user_message.lower() in {"yes", "y", "no", "n"}
                ):
                    await sessions.resolve_authorizations(
                        session_id,
                        approved=user_message.lower() in {"yes", "y"},
                    )
                    try:
                        view = await drive_with_console_interrupts(
                            sessions,
                            session_id,
                            input_queue,
                        )
                    except _ConsoleInputClosed:
                        print("\n已退出。")
                        return
                    except MODEL_DECISION_ERRORS as exc:
                        print(f"\n模型调用失败：{exc}")
                        continue
                    _print_runtime_status(view)
                    continue
                if view.terminal:
                    print("当前 Session 已结束，输入 /new。")
                    continue
                await sessions.receive_user_message(
                    session_id,
                    user_message,
                    delivery_id=f"user-{uuid4().hex}",
                )
                try:
                    view = await drive_with_console_interrupts(
                        sessions,
                        session_id,
                        input_queue,
                    )
                except _ConsoleInputClosed:
                    print("\n已退出。")
                    return
                except MODEL_DECISION_ERRORS as exc:
                    print(f"\n模型调用失败：{exc}")
                    continue
                _print_runtime_status(view)
        finally:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
