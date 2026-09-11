from __future__ import annotations

import json
import os
from pathlib import Path
import time

from helperme.config import AssistantConfig
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall


class ProcessLlm:
    def __init__(self, workspace):
        self.workspace = workspace

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def chat(self, messages, model, *, tools=None):
        images = [
            part
            for message in messages
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part["type"] == "image"
        ]
        if images:
            (self.workspace / "received-images.json").write_text(
                json.dumps(images), encoding="utf-8"
            )
        text = json.dumps(messages)
        names = {tool["function"]["name"] for tool in tools}
        if "CRASH_PROCESS" in text:
            raise RuntimeError("intentional worker crash")
        if "BLOCK_PROCESS" in text or "report" in names:
            (self.workspace / f"blocked-{os.getpid()}").touch()
            while not (self.workspace / "release").exists():
                time.sleep(0.02)
        calls = ()
        if "report" in names:
            if "READ_THEN_REPORT" in text and not any(
                m["role"] == "assistant" for m in messages
            ):
                calls = (ToolCall("read-one", "read_file", '{"path":"input.txt"}'),)
            else:
                calls = (ToolCall("report-one", "report", '{"summary":"child done"}'),)
        elif "DELEGATE_CHILDREN" in text and not any(
            m["role"] == "assistant" for m in messages
        ):
            calls = tuple(
                ToolCall(f"delegate-{i}", "delegate", '{"task":"read something"}')
                for i in range(2)
            )
        return LLMCallResult(
            LLMResponse(content="" if calls else "done", calls=calls),
            LLMUsage(input_tokens=10, output_tokens=5),
        )


def config_for(workspace: Path):
    return AssistantConfig(
        model_name="test",
        workspace_root=workspace,
        full_access=False,
        model_context_limit=200000,
        input_budget_ratio=0.9,
        llm=ProcessLlm(workspace),
    )


def interrupted_read_config(workspace: Path):
    from helperme.assistant.host import worker
    from helperme.runtime import ToolBinding

    build = worker.build_assistant_assembly

    async def read(context, arguments):
        marker = workspace / "read-started"
        if not marker.exists():
            marker.touch()
            # Kill without a Python except path; recovery must report the interruption.
            os._exit(1)
        (workspace / "read-retried").touch()
        return {"ok": True, "code": "FILE_READ", "data": "read successfully"}

    async def assembly(*args, **kwargs):
        result = await build(*args, **kwargs)
        result.runtime.bind_tool("read_file", ToolBinding(read))
        return result

    worker.build_assistant_assembly = assembly
    return config_for(workspace)


def failing_startup_config(workspace: Path, stage: str):
    """Fail once so the parent can subsequently start with the same factory."""
    from helperme.assistant.host import worker

    marker = workspace / f"failed-{stage}"
    if marker.exists():
        return config_for(workspace)
    marker.touch()
    if stage == "config":
        raise RuntimeError("config initialization failed")
    if stage == "assembly":

        async def fail(*args, **kwargs):
            raise RuntimeError("assembly initialization failed")

        worker.build_assistant_assembly = fail
    config = config_for(workspace)
    if stage == "client":

        async def enter(self):
            raise RuntimeError("client initialization failed")

        ProcessLlm.__aenter__ = enter
    return config


def failing_request_config(workspace: Path):
    from helperme.assistant.host import worker

    build = worker.build_assistant_assembly

    async def assembly(*args, **kwargs):
        result = await build(*args, **kwargs)

        async def fail(session_id, **kwargs):
            raise RuntimeError("application request failed")

        result.sessions.resolve_authorizations = fail
        return result

    worker.build_assistant_assembly = assembly
    return config_for(workspace)


def delegate_startup_failure_config(workspace: Path):
    import multiprocessing

    if "/sub-" in multiprocessing.current_process().name:
        raise RuntimeError("child initialization failed")
    return config_for(workspace)
