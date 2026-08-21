from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from adapters.runtime_host import (
    InterruptFlag,
    JournalBackedLlmDecisionMaker,
    build_runtime_tools,
    drive_until_idle,
    project_chat_messages,
)
from agent_runtime import AgentRuntime, MemoryJournal, RuntimeStatus
from core.agent_application import AgentApplication
from core.agent_workspace import AgentWorkspace
from core.composition import create_agent_application
from core.environment import FilesystemAccessMode
from core.model_call.client import LLMClient
from core.model_call.config import AppConfig, RuntimeConfig, WorkspaceConfig, load_app_config
from core.tools_runtime.turn_runtime import TurnStatus


CHAT_PROMPT = "只用一句话介绍你自己。不要调用任何工具。"
FILE_PROMPT = (
    "工作区有 notes.txt、config.json、readme.md。"
    "请先读取这三个文件，然后："
    "1) 把 notes.txt 里的 alpha 从 1 改成 9；"
    "2) 把 config.json 的 enabled 从 false 改成 true。"
    "不要改 readme.md。"
    "完成后必须根据磁盘实际内容总结改了什么。"
    "不要执行无关命令，不要安装依赖。"
)
INTERRUPT_PROMPT = (
    "请依次读取 notes.txt、config.json、readme.md，"
    "再新建 summary.md，把三个文件的关键信息写进去。"
    "每一步都先读再写。不要执行无关命令。"
)
CONTINUE_PROMPT = "请从中断处继续，完成剩余工作。"
MAX_STEPS = 16
CONTENT_PREVIEW = 400


FIXTURE_FILES = {
    "notes.txt": "alpha=1\nbeta=2\ntheme=compare\n",
    "config.json": '{\n  "name": "compare-fixture",\n  "enabled": false,\n  "count": 3\n}\n',
    "readme.md": "Runtime vs Core compare fixture.\nKeep this file unchanged.\n",
}


class RecordingLLM:
    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.calls: list[dict[str, object]] = []
        self.first_tool_step = asyncio.Event()

    async def __aenter__(self) -> "RecordingLLM":
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return await self._inner.__aexit__(exc_type, exc, traceback)

    async def close(self) -> None:
        await self._inner.close()

    async def chat(self, messages, model, tools=None):
        index = len(self.calls) + 1
        started = time.monotonic()
        print(
            f"  model call {index} start; messages={len(messages)}",
            flush=True,
        )
        result = await self._inner.chat(messages, model, tools)
        elapsed = time.monotonic() - started
        record = {
            "index": index,
            "elapsed_s": round(elapsed, 2),
            "n_messages": len(messages),
            "roles": [message.get("role") for message in messages],
            "has_tool_result": any(
                message.get("role") == "tool" for message in messages
            ),
            "tool_result_previews": _tool_result_previews(messages),
            "interrupt_in_context": _interrupt_in_messages(messages),
            "response_content": (result.response.content or "")[:CONTENT_PREVIEW],
            "response_tools": [
                {"name": call.name, "arguments": call.arguments[:300]}
                for call in result.response.calls
            ],
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "messages_tail": [_preview_message(message) for message in messages[-4:]],
        }
        self.calls.append(record)
        if result.response.calls:
            self.first_tool_step.set()
        print(
            f"  model call {index} done in {elapsed:.1f}s; "
            f"tools={[call.name for call in result.response.calls] or '-'}",
            flush=True,
        )
        return result


class RecordingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def emit(self, text: str) -> None:
        self.texts.append(text)
        preview = text.replace("\n", " ")[:160]
        print(f"  deliver: {preview}", flush=True)

    def __call__(self, text: str) -> None:
        self.emit(text)


def _preview_message(message: dict) -> dict[str, object]:
    preview: dict[str, object] = {"role": message.get("role")}
    content = message.get("content")
    if isinstance(content, str):
        preview["content"] = content[:CONTENT_PREVIEW]
    calls = message.get("tool_calls") or []
    if calls:
        preview["tool_calls"] = [
            call.get("function", {}).get("name")
            for call in calls
        ]
    if message.get("role") == "tool":
        preview["tool_call_id"] = message.get("tool_call_id")
    return preview


def _tool_result_previews(messages: list[dict]) -> list[str]:
    previews = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            previews.append(content[:180])
    return previews[-4:]


def _interrupt_in_messages(messages: list[dict]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and "[interrupt]" in content:
            return True
        if isinstance(content, str) and "运行已在安全点中断" in content:
            return True
    return False


def write_fixture(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in FIXTURE_FILES.items():
        (root / name).write_text(content, encoding="utf-8")
    summary = root / "summary.md"
    if summary.exists():
        summary.unlink()


def workspace_snapshot(root: Path) -> dict[str, str | None]:
    snapshot = {}
    for name in (*FIXTURE_FILES, "summary.md"):
        path = root / name
        snapshot[name] = (
            path.read_text(encoding="utf-8") if path.exists() else None
        )
    return snapshot


def file_task_checks(snapshot: dict[str, str | None]) -> dict[str, bool]:
    notes = snapshot.get("notes.txt") or ""
    config = snapshot.get("config.json") or ""
    readme = snapshot.get("readme.md") or ""
    return {
        "notes_alpha_is_9": "alpha=9" in notes,
        "notes_beta_unchanged": "beta=2" in notes,
        "config_enabled_true": '"enabled":true' in config.replace(" ", ""),
        "readme_unchanged": readme == FIXTURE_FILES["readme.md"],
    }


def next_step_clarity(calls: list[dict[str, object]]) -> dict[str, object]:
    tool_call_indexes = [
        index
        for index, call in enumerate(calls)
        if call["response_tools"]
    ]
    if not tool_call_indexes:
        return {"had_tool_step": False}
    first = tool_call_indexes[0]
    if first + 1 >= len(calls):
        return {
            "had_tool_step": True,
            "had_followup_call": False,
        }
    followup = calls[first + 1]
    previews = followup["tool_result_previews"]
    issued = len(calls[first]["response_tools"])
    followup_tool_messages = list(followup["roles"]).count("tool")
    return {
        "had_tool_step": True,
        "had_followup_call": True,
        "issued_tools_in_first_step": issued,
        "followup_tool_message_count": followup_tool_messages,
        "followup_sees_all_parallel_results": followup_tool_messages >= issued,
        "followup_has_tool_result": bool(followup["has_tool_result"]),
        "followup_tool_result_nonempty": bool(previews),
        "followup_sees_ok_or_content": any(
            '"ok"' in preview or "alpha=" in preview or "enabled" in preview
            for preview in previews
        ),
        "followup_roles": followup["roles"],
    }


def interrupt_clarity(
    calls: list[dict[str, object]],
    interrupt_after_call: int | None,
) -> dict[str, object]:
    if interrupt_after_call is None:
        return {"interrupt_fired": False}
    later = [
        call for call in calls
        if call["index"] > interrupt_after_call
    ]
    if not later:
        return {
            "interrupt_fired": True,
            "had_call_after_interrupt": False,
        }
    first_after = later[0]
    return {
        "interrupt_fired": True,
        "had_call_after_interrupt": True,
        "calls_after_interrupt": len(later),
        "interrupt_visible_in_next_call": bool(
            first_after["interrupt_in_context"]
        ),
        "prior_tool_results_still_visible": bool(
            first_after["has_tool_result"]
        ),
        "next_call_roles": first_after["roles"],
        "next_call_tools": first_after["response_tools"],
        "next_call_content": first_after["response_content"],
    }


def tool_names(calls: list[dict[str, object]]) -> list[str]:
    names = []
    for call in calls:
        for tool in call["response_tools"]:
            names.append(tool["name"])
    return names


def compare_config(app_config: AppConfig, workspace_root: Path) -> AppConfig:
    return AppConfig(
        model=app_config.model,
        workspace=WorkspaceConfig(root=workspace_root, full_access=False),
        runtime=RuntimeConfig(
            max_steps=MAX_STEPS,
            max_goal_turns=app_config.runtime.max_goal_turns,
            model_context_limit=app_config.runtime.model_context_limit,
            input_budget_ratio=app_config.runtime.input_budget_ratio,
        ),
    )


def create_core_application(
    app_config: AppConfig,
    agent_root: Path,
    workspace_root: Path,
    llm: RecordingLLM,
    sink: RecordingSink,
) -> AgentApplication:
    return create_agent_application(
        model=app_config.model.name,
        model_context_limit=app_config.runtime.model_context_limit,
        agent_workspace=AgentWorkspace(agent_root),
        workspace_roots={"project": workspace_root},
        input_budget_ratio=app_config.runtime.input_budget_ratio,
        llm_client=llm,
        progress_sink=sink,
        filesystem_access_mode=FilesystemAccessMode.SCOPED,
        default_max_steps=MAX_STEPS,
    )


async def run_core_turn(
    application: AgentApplication,
    session_id: str,
    prompt: str,
    *,
    resume: bool = False,
    interrupt_after_first_tools: bool = False,
    llm: RecordingLLM | None = None,
) -> dict[str, object]:
    turn_id = f"turn-{uuid4().hex}"
    started = time.monotonic()
    interrupt_after_call = None
    if interrupt_after_first_tools:
        assert llm is not None
        llm.first_tool_step.clear()

        async def execute():
            return await application.start(session_id, turn_id, prompt)

        task = asyncio.create_task(execute())
        try:
            await asyncio.wait_for(llm.first_tool_step.wait(), timeout=180)
            interrupt_after_call = len(llm.calls)
            application.request_interrupt(session_id, "compare_interrupt")
            print("  core interrupt requested", flush=True)
        except (asyncio.TimeoutError, ValueError) as exc:
            print(f"  core interrupt not sent: {exc}", flush=True)
        outcome = await task
    elif resume:
        outcome = await application.resume(session_id, turn_id, prompt)
    else:
        outcome = await application.start(session_id, turn_id, prompt)
    session = application._session_runtime.sessions[session_id]
    messages = session.conversation.protocol_messages()
    return {
        "status": outcome.result.status.value,
        "answer": outcome.result.answer,
        "final_reason": outcome.result.final_reason,
        "elapsed_s": round(time.monotonic() - started, 2),
        "protocol_roles": [message.get("role") for message in messages],
        "interrupt_after_call": interrupt_after_call,
    }


async def build_runtime(
    app_config: AppConfig,
    llm: RecordingLLM,
    sink: RecordingSink,
) -> tuple[AgentRuntime, str]:
    tool_bindings, model_tools = await build_runtime_tools(app_config, sink)
    journal = MemoryJournal()
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
    return runtime, f"stream-{uuid4().hex}"


async def run_runtime_turn(
    runtime: AgentRuntime,
    stream_id: str,
    prompt: str,
    *,
    interrupt: InterruptFlag | None = None,
    interrupt_after_first_tools: bool = False,
    llm: RecordingLLM | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    await runtime.receive_user_message(
        stream_id,
        prompt,
        delivery_id=f"user-{uuid4().hex}",
    )
    interrupt_after_call = None
    flag = interrupt or InterruptFlag()
    if interrupt_after_first_tools:
        assert llm is not None
        llm.first_tool_step.clear()

        async def execute():
            return await drive_until_idle(
                runtime,
                stream_id,
                max_steps=MAX_STEPS,
                interrupt_requested=flag,
            )

        task = asyncio.create_task(execute())
        try:
            await asyncio.wait_for(llm.first_tool_step.wait(), timeout=180)
            interrupt_after_call = len(llm.calls)
            flag.set()
            print("  runtime interrupt requested", flush=True)
        except asyncio.TimeoutError:
            print("  runtime interrupt not sent: no tool step", flush=True)
        result = await task
    else:
        result = await drive_until_idle(
            runtime,
            stream_id,
            max_steps=MAX_STEPS,
            interrupt_requested=flag,
        )
    state = result.state
    events = await runtime._journal.snapshot(stream_id)
    messages = project_chat_messages(
        events,
        tuple(event.event_id for event in events),
    )
    return {
        "status": state.status.value,
        "paused": result.paused,
        "waiting_for": list(state.waiting_for),
        "elapsed_s": round(time.monotonic() - started, 2),
        "event_kinds": [type(event.payload).__name__ for event in events],
        "projected_roles": [message.get("role") for message in messages],
        "interrupt_after_call": interrupt_after_call,
        "step_count": len(state.steps),
    }


async def run_core_scenarios(
    app_config: AppConfig,
    agent_root: Path,
    workspace_root: Path,
) -> dict[str, object]:
    print("\n=== CORE engine ===", flush=True)
    inner = LLMClient(app_config.model)
    llm = RecordingLLM(inner)
    sink = RecordingSink()
    results: dict[str, object] = {"engine": "core"}
    async with llm:
        application = create_core_application(
            app_config,
            agent_root,
            workspace_root,
            llm,
            sink,
        )
        async with application:
            session_id = application.create_session(f"core-{uuid4().hex}")

            print("\n-- core chat --", flush=True)
            chat_calls_before = len(llm.calls)
            results["chat"] = await run_core_turn(
                application,
                session_id,
                CHAT_PROMPT,
            )
            results["chat"]["model_calls"] = llm.calls[chat_calls_before:]
            results["chat"]["delivered"] = list(sink.texts)
            sink.texts.clear()

            write_fixture(workspace_root)
            print("\n-- core file task --", flush=True)
            file_session = application.create_session(f"core-file-{uuid4().hex}")
            file_before = len(llm.calls)
            results["file"] = await run_core_turn(
                application,
                file_session,
                FILE_PROMPT,
            )
            file_calls = llm.calls[file_before:]
            results["file"]["model_calls"] = file_calls
            results["file"]["delivered"] = list(sink.texts)
            results["file"]["tool_names"] = tool_names(file_calls)
            results["file"]["next_step_clarity"] = next_step_clarity(file_calls)
            results["file"]["workspace"] = workspace_snapshot(workspace_root)
            results["file"]["checks"] = file_task_checks(
                results["file"]["workspace"]
            )
            sink.texts.clear()

            write_fixture(workspace_root)
            print("\n-- core interrupt --", flush=True)
            interrupt_session = application.create_session(
                f"core-int-{uuid4().hex}"
            )
            interrupt_before = len(llm.calls)
            interrupted = await run_core_turn(
                application,
                interrupt_session,
                INTERRUPT_PROMPT,
                interrupt_after_first_tools=True,
                llm=llm,
            )
            interrupt_calls = llm.calls[interrupt_before:]
            interrupted["model_calls"] = interrupt_calls
            interrupted["delivered"] = list(sink.texts)
            interrupted["tool_names"] = tool_names(interrupt_calls)
            interrupted["interrupt_clarity"] = interrupt_clarity(
                interrupt_calls,
                interrupted["interrupt_after_call"],
            )
            interrupted["workspace_after_interrupt"] = workspace_snapshot(
                workspace_root
            )
            sink.texts.clear()

            continued = None
            if interrupted["status"] == TurnStatus.INTERRUPTED.value:
                print("\n-- core resume --", flush=True)
                resume_before = len(llm.calls)
                continued = await run_core_turn(
                    application,
                    interrupt_session,
                    CONTINUE_PROMPT,
                    resume=True,
                )
                resume_calls = llm.calls[resume_before:]
                continued["model_calls"] = resume_calls
                continued["delivered"] = list(sink.texts)
                continued["tool_names"] = tool_names(resume_calls)
                continued["next_step_clarity"] = next_step_clarity(
                    interrupt_calls + resume_calls
                )
            results["interrupt"] = {
                "interrupted": interrupted,
                "continued": continued,
                "workspace_final": workspace_snapshot(workspace_root),
            }
    return results


async def run_runtime_scenarios(
    app_config: AppConfig,
) -> dict[str, object]:
    print("\n=== RUNTIME engine ===", flush=True)
    inner = LLMClient(app_config.model)
    llm = RecordingLLM(inner)
    sink = RecordingSink()
    results: dict[str, object] = {"engine": "runtime"}
    async with llm:
        print("\n-- runtime chat --", flush=True)
        runtime, stream_id = await build_runtime(app_config, llm, sink)
        chat_before = len(llm.calls)
        results["chat"] = await run_runtime_turn(
            runtime,
            stream_id,
            CHAT_PROMPT,
        )
        results["chat"]["model_calls"] = llm.calls[chat_before:]
        results["chat"]["delivered"] = list(sink.texts)
        sink.texts.clear()

        write_fixture(app_config.workspace.root)
        print("\n-- runtime file task --", flush=True)
        file_runtime, file_stream = await build_runtime(app_config, llm, sink)
        file_before = len(llm.calls)
        results["file"] = await run_runtime_turn(
            file_runtime,
            file_stream,
            FILE_PROMPT,
        )
        file_calls = llm.calls[file_before:]
        results["file"]["model_calls"] = file_calls
        results["file"]["delivered"] = list(sink.texts)
        results["file"]["tool_names"] = tool_names(file_calls)
        results["file"]["next_step_clarity"] = next_step_clarity(file_calls)
        results["file"]["workspace"] = workspace_snapshot(
            app_config.workspace.root
        )
        results["file"]["checks"] = file_task_checks(
            results["file"]["workspace"]
        )
        sink.texts.clear()

        write_fixture(app_config.workspace.root)
        print("\n-- runtime interrupt --", flush=True)
        int_runtime, int_stream = await build_runtime(app_config, llm, sink)
        interrupt_before = len(llm.calls)
        interrupted = await run_runtime_turn(
            int_runtime,
            int_stream,
            INTERRUPT_PROMPT,
            interrupt_after_first_tools=True,
            llm=llm,
        )
        interrupt_calls = llm.calls[interrupt_before:]
        interrupted["model_calls"] = interrupt_calls
        interrupted["delivered"] = list(sink.texts)
        interrupted["tool_names"] = tool_names(interrupt_calls)
        interrupted["interrupt_clarity"] = interrupt_clarity(
            interrupt_calls,
            interrupted["interrupt_after_call"],
        )
        interrupted["workspace_after_interrupt"] = workspace_snapshot(
            app_config.workspace.root
        )
        sink.texts.clear()

        continued = None
        state = await int_runtime.state(int_stream)
        can_continue = state.status not in {
            RuntimeStatus.COMPLETED,
            RuntimeStatus.TERMINATED,
        }
        if can_continue:
            print("\n-- runtime continue --", flush=True)
            resume_before = len(llm.calls)
            continued = await run_runtime_turn(
                int_runtime,
                int_stream,
                CONTINUE_PROMPT,
            )
            resume_calls = llm.calls[resume_before:]
            continued["model_calls"] = resume_calls
            continued["delivered"] = list(sink.texts)
            continued["tool_names"] = tool_names(resume_calls)
        results["interrupt"] = {
            "interrupted": interrupted,
            "continued": continued,
            "workspace_final": workspace_snapshot(app_config.workspace.root),
        }
        sink.texts.clear()

        print("\n-- runtime /stop --", flush=True)
        stop_runtime, stop_stream = await build_runtime(app_config, llm, sink)
        stop_before = len(llm.calls)
        chat = await run_runtime_turn(
            stop_runtime,
            stop_stream,
            CHAT_PROMPT,
        )
        await stop_runtime.receive_termination(
            stop_stream,
            "console_stop",
            delivery_id=f"stop-{uuid4().hex}",
        )
        after = await drive_until_idle(
            stop_runtime,
            stop_stream,
            max_steps=MAX_STEPS,
        )
        after_state = after.state
        results["stop"] = {
            "before_status": chat["status"],
            "after_status": after_state.status.value,
            "console_would_refuse_new_message": after_state.status in {
                RuntimeStatus.COMPLETED,
                RuntimeStatus.TERMINATED,
            },
            "model_calls": llm.calls[stop_before:],
            "delivered": list(sink.texts),
        }
    return results


def compact_engine(result: dict[str, object]) -> dict[str, object]:
    chat = result["chat"]
    file_task = result["file"]
    interrupt = result["interrupt"]
    compact = {
        "engine": result["engine"],
        "chat": {
            "status": chat["status"],
            "elapsed_s": chat["elapsed_s"],
            "delivered": chat.get("delivered"),
            "answer": chat.get("answer"),
            "waiting_for": chat.get("waiting_for"),
            "model_call_count": len(chat["model_calls"]),
            "used_tools": tool_names(chat["model_calls"]),
        },
        "file": {
            "status": file_task["status"],
            "elapsed_s": file_task["elapsed_s"],
            "delivered": file_task.get("delivered"),
            "answer": file_task.get("answer"),
            "waiting_for": file_task.get("waiting_for"),
            "model_call_count": len(file_task["model_calls"]),
            "tool_names": file_task["tool_names"],
            "next_step_clarity": file_task["next_step_clarity"],
            "checks": file_task["checks"],
            "workspace": file_task["workspace"],
        },
        "interrupt": {
            "status": interrupt["interrupted"]["status"],
            "paused": interrupt["interrupted"].get("paused"),
            "waiting_for": interrupt["interrupted"].get("waiting_for"),
            "interrupt_clarity": interrupt["interrupted"]["interrupt_clarity"],
            "tool_names": interrupt["interrupted"]["tool_names"],
            "delivered": interrupt["interrupted"].get("delivered"),
            "answer": interrupt["interrupted"].get("answer"),
            "workspace_after_interrupt": interrupt["interrupted"][
                "workspace_after_interrupt"
            ],
            "continued_status": (
                interrupt["continued"]["status"]
                if interrupt["continued"] is not None
                else None
            ),
            "continued_delivered": (
                interrupt["continued"].get("delivered")
                if interrupt["continued"] is not None
                else None
            ),
            "workspace_final": interrupt["workspace_final"],
        },
    }
    if "stop" in result:
        compact["stop"] = {
            "before_status": result["stop"]["before_status"],
            "after_status": result["stop"]["after_status"],
            "console_would_refuse_new_message": result["stop"][
                "console_would_refuse_new_message"
            ],
        }
    return compact


async def async_main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    base_config = load_app_config()
    report_path = Path(__file__).resolve().parent / "runtime_vs_core_last_report.json"
    compare_root = Path(tempfile.mkdtemp(prefix="helperme-runtime-vs-core-"))
    core_project = compare_root / "core-project"
    runtime_project = compare_root / "runtime-project"
    agent_root = compare_root / "agent"
    write_fixture(core_project)
    write_fixture(runtime_project)
    core_config = compare_config(base_config, core_project)
    runtime_config = compare_config(base_config, runtime_project)

    started = time.monotonic()
    core_result = await run_core_scenarios(core_config, agent_root, core_project)
    runtime_result = await run_runtime_scenarios(runtime_config)
    compact = {
        "model": base_config.model.name,
        "max_steps": MAX_STEPS,
        "elapsed_s": round(time.monotonic() - started, 2),
        "fixture_root": str(compare_root),
        "core": compact_engine(core_result),
        "runtime": compact_engine(runtime_result),
        "raw": {
            "core": core_result,
            "runtime": runtime_result,
        },
    }
    report_path.write_text(
        json.dumps(compact, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    summary = {
        "report_path": str(report_path),
        "model": compact["model"],
        "elapsed_s": compact["elapsed_s"],
        "core": compact["core"],
        "runtime": compact["runtime"],
    }
    print("\n\n=== COMPARE SUMMARY ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(async_main())
