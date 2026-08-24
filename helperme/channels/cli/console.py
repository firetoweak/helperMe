from __future__ import annotations

import asyncio
import sys
import time
from uuid import uuid4

from helperme.assistant.streams import (
    AssistantStreams,
    StreamNotFoundError,
    StreamView,
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


class _InputPrompt:
    def __init__(self) -> None:
        self._ready = asyncio.Event()

    def ask(self) -> None:
        self._ready.set()

    async def wait(self) -> None:
        await self._ready.wait()
        self._ready.clear()


def _poll_line(timeout: float, buffer: list[str]) -> str | None:
    """Read a line without printing 你：. None means timeout."""

    if sys.platform == "win32":
        import msvcrt

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not msvcrt.kbhit():
                time.sleep(0.03)
                continue
            char = msvcrt.getwch()
            if char in {"\x00", "\xe0"}:
                msvcrt.getwch()
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            if char in {"\r", "\n"}:
                print(flush=True)
                text = "".join(buffer).strip()
                buffer.clear()
                return text
            if char == "\x08":
                if buffer:
                    buffer.pop()
                    print("\b \b", end="", flush=True)
                continue
            buffer.append(char)
            print(char, end="", flush=True)
        return None

    import select

    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    line = sys.stdin.readline()
    if line == "":
        return None
    return line.strip()


async def _running_input(
    drive: asyncio.Task[StreamView],
    input_queue: asyncio.Queue[str | None],
    *,
    poll_keyboard: bool,
) -> str | None:
    incoming = asyncio.create_task(input_queue.get())
    keyboard_buffer: list[str] = []
    try:
        while not drive.done():
            watchers: set[asyncio.Task[object]] = {drive, incoming}
            poll: asyncio.Task[str | None] | None = None
            if poll_keyboard:
                poll = asyncio.create_task(asyncio.to_thread(
                    _poll_line,
                    0.15,
                    keyboard_buffer,
                ))
                watchers.add(poll)
            done, _ = await asyncio.wait(
                watchers,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if poll is not None and poll not in done:
                poll.cancel()
                try:
                    await poll
                except asyncio.CancelledError:
                    pass
            elif poll is not None:
                line = poll.result()
                if line is not None:
                    return line
                continue
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
    streams: AssistantStreams,
    stream_id: str,
    input_queue: asyncio.Queue[str | None],
    *,
    evaluate_completion: bool = True,
    prompt: _InputPrompt | None = None,
) -> StreamView:
    """Drive one Stream while concurrent CLI text becomes a durable interrupt."""

    print("\n● 运行中", flush=True)
    drive = asyncio.create_task(streams.drive(
        stream_id,
        evaluate_completion=evaluate_completion,
    ))
    try:
        while not drive.done():
            text = await _running_input(
                drive,
                input_queue,
                poll_keyboard=prompt is not None,
            )
            if drive.done() and text is None:
                break
            if text is None:
                if drive.done():
                    break
                raise _ConsoleInputClosed
            if not text:
                continue
            if text == "/stop":
                await streams.request_termination(
                    stream_id,
                    "console_stop",
                    delivery_id=f"stop-{uuid4().hex}",
                )
            else:
                await streams.receive_interrupt(
                    stream_id,
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
    prompt: _InputPrompt,
) -> None:
    while True:
        await prompt.wait()
        try:
            text = await asyncio.to_thread(input, "\n你：")
        except EOFError:
            await queue.put(None)
            return
        await queue.put(text.strip())


def _print_runtime_status(view: StreamView) -> None:
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

    async with bootstrap_assistant(sink) as app:
        config = app.config
        streams = app.streams
        mcp_console = McpConsoleAdapter(app.mcp_service)
        skill_console = SkillConsoleAdapter(app.skill_service)
        stream_id = f"stream-{uuid4().hex}"
        await streams.create(stream_id)
        input_queue: asyncio.Queue[str | None] = asyncio.Queue()
        access = "整台电脑" if config.full_access else "配置的 Workspace"
        print(f"HelperMe 已启动。model={config.model_name}")
        print(f"工作区：{access}")
        print(f"当前对话：{stream_id}")
        print("/new 新对话    /resume <id> 恢复    /stop 结束")
        print("/mcp  /skill  管理外部能力")
        print("直接输入任务。运行中再输入会打断当前任务。")
        print("Ctrl+C 或 Ctrl+D 退出。")

        prompt = _InputPrompt()
        reader = asyncio.create_task(read_console_input(input_queue, prompt))
        try:
            separate_turns = False
            while True:
                if separate_turns:
                    print(f"\n{_TURN_RULE}", flush=True)
                    separate_turns = False
                prompt.ask()
                user_message = await input_queue.get()
                if user_message is None:
                    print("\n已退出。")
                    return
                if not user_message:
                    continue
                separate_turns = True
                if user_message == "/new":
                    stream_id = f"stream-{uuid4().hex}"
                    await streams.create(stream_id)
                    print(f"\n新 stream 已创建：{stream_id}")
                    continue
                if user_message == "/resume" or user_message.startswith(
                    "/resume "
                ):
                    parts = user_message.split(maxsplit=1)
                    if len(parts) != 2 or not parts[1].strip():
                        print("\n用法：/resume <stream_id>")
                        continue
                    target_stream_id = parts[1].strip()
                    try:
                        view = await streams.resume(target_stream_id)
                    except StreamNotFoundError:
                        print(f"\nStream 不存在：{target_stream_id}")
                        continue
                    except ToolsetLoadError as exc:
                        print(
                            f"\nStream 恢复失败：{exc.code}: {exc.message}"
                        )
                        continue
                    stream_id = target_stream_id
                    print(f"\n已恢复 stream：{stream_id}")
                    if view.should_drive:
                        try:
                            view = await drive_with_console_interrupts(
                                streams,
                                stream_id,
                                input_queue,
                                prompt=prompt,
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
                view = await streams.view(stream_id)
                if (
                    view.control_approval is not None
                    and user_message.lower() in {"yes", "y", "no", "n"}
                ):
                    message = await streams.resolve_control(
                        stream_id,
                        approved=user_message.lower() in {"yes", "y"},
                    )
                    print(f"\n控制面：{message}")
                    _print_runtime_status(await streams.view(stream_id))
                    continue
                if user_message == "/stop":
                    if view.terminal:
                        print("当前 stream 已经结束。")
                        continue
                    await streams.request_termination(
                        stream_id,
                        "console_stop",
                        delivery_id=f"stop-{uuid4().hex}",
                    )
                    try:
                        view = await drive_with_console_interrupts(
                            streams,
                            stream_id,
                            input_queue,
                            evaluate_completion=False,
                            prompt=prompt,
                        )
                    except _ConsoleInputClosed:
                        print("\n已退出。")
                        return
                    except MODEL_DECISION_ERRORS as exc:
                        print(f"\n模型调用失败：{exc}")
                        continue
                    _print_runtime_status(view)
                    continue
                if (
                    view.pending_authorization_ids
                    and user_message.lower() in {"yes", "y", "no", "n"}
                ):
                    await streams.resolve_authorizations(
                        stream_id,
                        approved=user_message.lower() in {"yes", "y"},
                    )
                    try:
                        view = await drive_with_console_interrupts(
                            streams,
                            stream_id,
                            input_queue,
                            prompt=prompt,
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
                    print("当前 stream 已结束，输入 /new。")
                    continue
                await streams.receive_user_message(
                    stream_id,
                    user_message,
                    delivery_id=f"user-{uuid4().hex}",
                )
                try:
                    view = await drive_with_console_interrupts(
                        streams,
                        stream_id,
                        input_queue,
                        prompt=prompt,
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
