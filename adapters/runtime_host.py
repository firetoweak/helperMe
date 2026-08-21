from __future__ import annotations

import asyncio
import json
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from adapters.delivery import (
    DELIVER_TOOL_NAME,
    deliver_binding,
    ensure_deliver,
)
from agent_runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    StepCommitted,
    ToolBinding,
    UserInterruptReceived,
    UserMessageReceived,
)
from agent_runtime.dispatcher import AttemptContext
from agent_runtime.events import Event
from agent_runtime.model import CanonicalState, CommandOutcome
from agent_runtime.state import DecisionFrame
from core.approval import ApprovalRequest
from core.environment import (
    EnvironmentSelection,
    LocalEnvironmentProvider,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
    discover_host_roots,
)
from core.model_call.client import LLMClient
from core.model_call.config import AppConfig, load_app_config
from core.model_call.types import LLMResponse
from core.prompt import DEFAULT_AGENT_PROMPT
from core.tool_registry import BUILTIN_TOOL_REGISTRY, ToolRegistry
from core.tools_runtime.tools_executor import ToolsExecutor
from tools import create_environment_tool_specs
from tools.powershell_runner import PowerShellCommandRunner


OUTCOME_JSON_LIMIT = 100_000


class InterruptFlag:
    def __init__(self) -> None:
        self._set = False

    def set(self) -> None:
        self._set = True

    def consume(self) -> bool:
        if not self._set:
            return False
        self._set = False
        return True


def decision_from_llm(response: LLMResponse) -> ModelDecision:
    requests: list[InvokeTool] = []
    for call in response.calls:
        if call.name == DELIVER_TOOL_NAME:
            raise ValueError("deliver is a product command, not a model tool")
        payload = json.loads(call.arguments)
        if type(payload) is not dict:
            raise ValueError("tool arguments must be a JSON object")
        requests.append(InvokeTool(call.name, tuple(payload.items())))
    return ModelDecision(
        content=response.content,
        command_requests=tuple(requests),
    )


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def _outcome_text(outcome: CommandOutcome) -> str:
    return json.dumps(
        {
            "status": outcome.status.value,
            "value": _jsonable(outcome.value),
            "error_type": outcome.error_type,
            "error_message": outcome.error_message,
        },
        ensure_ascii=False,
    )


def project_chat_messages(
    events: tuple[Event, ...],
    visible_event_ids: tuple[str, ...],
    system_prompt: str = DEFAULT_AGENT_PROMPT,
) -> list[dict[str, object]]:
    visible = set(visible_event_ids)
    messages: list[dict[str, object]] = [
        {"role": "system", "content": system_prompt},
    ]
    commands: dict[str, InvokeTool] = {}
    for event in events:
        if event.event_id not in visible:
            continue
        payload = event.payload
        if isinstance(payload, UserMessageReceived):
            messages.append({"role": "user", "content": payload.content})
            continue
        if isinstance(payload, UserInterruptReceived):
            reason = payload.reason or "interrupted"
            messages.append(
                {"role": "user", "content": f"[interrupt] {reason}"},
            )
            continue
        if isinstance(payload, StepCommitted):
            shown: list[dict[str, object]] = []
            for command in payload.step.commands:
                effect = command.effect
                if not isinstance(effect, InvokeTool):
                    continue
                commands[command.command_id] = effect
                if effect.name == DELIVER_TOOL_NAME:
                    continue
                shown.append({
                    "id": command.command_id,
                    "type": "function",
                    "function": {
                        "name": effect.name,
                        "arguments": json.dumps(
                            dict(effect.arguments),
                            ensure_ascii=False,
                        ),
                    },
                })
            content = payload.step.decision.content
            if not content and not shown:
                continue
            message: dict[str, object] = {
                "role": "assistant",
                "content": content or None,
            }
            if shown:
                message["tool_calls"] = shown
            messages.append(message)
            continue
        if isinstance(payload, CommandOutcomeReceived):
            effect = commands.get(payload.command_id)
            if effect is None or effect.name == DELIVER_TOOL_NAME:
                continue
            messages.append({
                "role": "tool",
                "tool_call_id": payload.command_id,
                "content": _outcome_text(payload.outcome),
            })
    return messages


def _bounded_outcome(value: object) -> object:
    encoded = json.dumps(_jsonable(value), ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= OUTCOME_JSON_LIMIT:
        return value
    return {
        "ok": False,
        "code": "RESULT_TOO_LARGE",
        "error": "tool result exceeds runtime outcome budget",
        "hint": "narrow path, offset, or limit and retry",
    }


def bind_executor_tools(executor: ToolsExecutor) -> dict[str, ToolBinding]:
    bindings: dict[str, ToolBinding] = {}
    for tool in executor.registry.get_tools():
        name = tool["function"]["name"]
        bindings[name] = ToolBinding(_executor_handler(executor, name))
    return bindings


def _executor_handler(executor: ToolsExecutor, name: str):
    async def handler(
        _context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> object:
        result = await executor.execute(
            name,
            json.dumps(dict(arguments), ensure_ascii=False),
        )
        if isinstance(result, ApprovalRequest):
            return {
                "ok": False,
                "code": "APPROVAL_NOT_WIRED",
                "error": f"approval required: {result.action}",
                "hint": "runtime harness does not wire approval yet",
            }
        return _bounded_outcome(result)

    return handler


class JournalBackedLlmDecisionMaker:
    def __init__(
        self,
        journal,
        llm: LLMClient,
        model: str,
        tool_schemas: list[dict[str, object]],
        system_prompt: str = DEFAULT_AGENT_PROMPT,
    ) -> None:
        self._journal = journal
        self._llm = llm
        self._model = model
        self._tool_schemas = tool_schemas
        self._system_prompt = system_prompt

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        events = await self._journal.snapshot(frame.state.stream_id)
        messages = project_chat_messages(
            events,
            frame.state.visible_event_ids,
            self._system_prompt,
        )
        result = await self._llm.chat(
            messages,
            self._model,
            tools=self._tool_schemas or None,
        )
        return ensure_deliver(decision_from_llm(result.response))


@dataclass(frozen=True, slots=True)
class DriveResult:
    state: CanonicalState
    paused: bool = False


async def drive_until_idle(
    runtime: AgentRuntime,
    stream_id: str,
    *,
    max_steps: int,
    interrupt_requested: InterruptFlag | None = None,
) -> DriveResult:
    steps = 0
    while True:
        if interrupt_requested is not None and interrupt_requested.consume():
            return DriveResult(await runtime.state(stream_id), paused=True)
        step = await runtime.advance(stream_id)
        if step is not None:
            steps += 1
        await runtime.dispatcher.wait_all()
        await runtime.finalize(stream_id)
        if interrupt_requested is not None and interrupt_requested.consume():
            return DriveResult(await runtime.state(stream_id), paused=True)
        state = await runtime.state(stream_id)
        if state.status in {
            RuntimeStatus.COMPLETED,
            RuntimeStatus.TERMINATED,
        }:
            return DriveResult(state)
        if (
            state.status is RuntimeStatus.WAITING
            and state.waiting_for == ("user_message",)
        ):
            return DriveResult(state)
        if steps >= max_steps:
            return DriveResult(state)
        if (
            state.status is RuntimeStatus.RUNNABLE
            or state.waiting_command_ids
        ):
            continue
        return DriveResult(state)


def _workspace_view(
    app_config: AppConfig,
) -> tuple[dict[str, Path], WorkspaceViewSnapshot]:
    workspace_roots = {"project": app_config.workspace.root}
    effective = dict(workspace_roots)
    if app_config.workspace.full_access:
        host_roots = {
            root.root_id: root.path for root in discover_host_roots()
        }
        duplicated = effective.keys() & host_roots.keys()
        if duplicated:
            raise ValueError(
                "显式 workspace root 与 Host root 名称冲突: "
                f"{sorted(duplicated)}"
            )
        effective.update(host_roots)
    task_root_ids = set(workspace_roots)
    view = WorkspaceViewSnapshot(tuple(
        RootBinding(
            root_id=name,
            scope=(
                WorkspaceScope.TASK
                if name in task_root_ids
                else WorkspaceScope.HOST
            ),
            path=root,
        )
        for name, root in effective.items()
    ))
    return workspace_roots, view


def _tool_registry(app_config: AppConfig, binding) -> ToolRegistry:
    import tools  # noqa: F401

    registry = BUILTIN_TOOL_REGISTRY.clone()
    for spec in create_environment_tool_specs(binding):
        registry.register(spec)
    return registry


async def build_runtime_tools(
    app_config: AppConfig,
    sink,
) -> tuple[dict[str, ToolBinding], list[dict[str, object]]]:
    workspace_roots, view = _workspace_view(app_config)
    command_runner = PowerShellCommandRunner()
    provider = LocalEnvironmentProvider(
        command_runner,
        shell_path=command_runner.executable,
    )
    binding = await provider.attach(EnvironmentSelection(
        environment_id=provider.environment_id,
        workspace_view=view,
        cwd=str(next(iter(workspace_roots.values())).resolve()),
    ))
    registry = _tool_registry(app_config, binding)
    executor = ToolsExecutor(registry)
    return (
        {
            **bind_executor_tools(executor),
            **deliver_binding(sink),
        },
        registry.get_tools(),
    )


def _print_runtime_status(
    state: CanonicalState,
    *,
    paused: bool = False,
) -> None:
    if paused:
        print("已暂停自动推进。输入下一句继续，或 /stop 结束。")
    print(f"Runtime 状态：{state.status.value}")
    if state.waiting_for:
        print("等待：" + ", ".join(state.waiting_for))


async def run_runtime_console() -> None:
    app_config = load_app_config()
    llm = LLMClient(app_config.model)
    journal = MemoryJournal()

    def sink(text: str) -> None:
        print(f"\n助手：{text}")

    tool_bindings, model_tools = await build_runtime_tools(app_config, sink)
    runtime = AgentRuntime(
        journal,
        JournalBackedLlmDecisionMaker(
            journal,
            llm,
            app_config.model.name,
            model_tools,
        ),
        tool_bindings,
    )
    stream_id = f"stream-{uuid4().hex}"
    interrupt = InterruptFlag()
    access = (
        "整台电脑"
        if app_config.workspace.full_access
        else "配置的 Workspace"
    )
    print(f"Runtime 对照入口已启动。model={app_config.model.name}")
    print(f"文件工具访问：{access}")
    print(f"单次推进最大 Step 数：{app_config.runtime.max_steps}")
    print("本入口不含 Goal / MCP / Skill / Approval / Artifact。")
    print("输入任务开始；运行期间按 Ctrl+C 请求中断（当前 Step 提交后生效）。")
    print("在输入提示处按 Ctrl+C 或 Ctrl+D 退出。")
    print("新建 stream：输入 /new")
    print("结束当前 stream：输入 /stop")

    async with llm:
        while True:
            try:
                user_message = (
                    await asyncio.to_thread(input, "\n你：")
                ).strip()
            except EOFError:
                print("\n已退出。")
                return
            if not user_message:
                continue
            if user_message == "/new":
                stream_id = f"stream-{uuid4().hex}"
                print("\n新 stream 已创建。")
                continue
            state = await runtime.state(stream_id)
            if user_message == "/stop":
                if state.status in {
                    RuntimeStatus.COMPLETED,
                    RuntimeStatus.TERMINATED,
                }:
                    print("当前 stream 已经结束。")
                    continue
                await runtime.receive_termination(
                    stream_id,
                    "console_stop",
                    delivery_id=f"stop-{uuid4().hex}",
                )
                result = await drive_until_idle(
                    runtime,
                    stream_id,
                    max_steps=app_config.runtime.max_steps,
                    interrupt_requested=interrupt,
                )
                _print_runtime_status(result.state, paused=result.paused)
                continue
            if state.status in {
                RuntimeStatus.COMPLETED,
                RuntimeStatus.TERMINATED,
            }:
                print("当前 stream 已结束，输入 /new。")
                continue
            await runtime.receive_user_message(
                stream_id,
                user_message,
                delivery_id=f"user-{uuid4().hex}",
            )
            previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda *_: interrupt.set())
            try:
                result = await drive_until_idle(
                    runtime,
                    stream_id,
                    max_steps=app_config.runtime.max_steps,
                    interrupt_requested=interrupt,
                )
            except Exception as exc:
                print(f"\nRuntime 入口错误：{exc}")
                continue
            finally:
                signal.signal(signal.SIGINT, previous)
            _print_runtime_status(result.state, paused=result.paused)
