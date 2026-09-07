from __future__ import annotations

import asyncio
import json
from pathlib import Path

from helperme.config import AssistantConfig
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall


HANDOFF = """## 用户要什么
用户要求讨论并保留 TAIL_KEEP。
## 目前做到哪里
旧模型已回应早期输入；完成声明未经独立验证。
## 还有什么未解决
等待用户下一条输入。
## 接手所需的证据与入口
按来源回读旧会话。
"""


class CompactLlm:
    def __init__(self, workspace):
        self.workspace = workspace

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def chat(self, messages, model, *, tools=None):
        names = {t["function"]["name"] for t in tools or []}
        if "submit_handoff" in names:
            (self.workspace / "compact_started").touch()
            if (self.workspace / "fail_compact").exists():
                from helperme.llm.api import LLMProviderError

                raise LLMProviderError("compactor provider failed")
            while not (self.workspace / "release_compact").exists():
                await asyncio.sleep(0.02)
            task = json.loads(messages[1]["content"])["data"]
            results = [
                json.loads(m["content"])["value"]
                for m in messages
                if m["role"] == "tool"
            ]
            if not results or results[-1]["next_offset"] is not None:
                offset = 0 if not results else results[-1]["next_offset"]
                call = ToolCall(
                    "read",
                    "read_compact_source",
                    json.dumps(
                        {
                            "source": task["source"],
                            "kind": "view",
                            "reference": "",
                            "offset": offset,
                            "limit": 12000,
                        }
                    ),
                )
            else:
                call = ToolCall(
                    "submit", "submit_handoff", json.dumps({"handoff": HANDOFF})
                )
            response = LLMResponse(content="", calls=(call,))
        else:
            # Record actual requests to prove S1 sees the tail and no old execution replays.
            with (self.workspace / "requests.jsonl").open(
                "a", encoding="utf-8"
            ) as file:
                file.write(json.dumps(messages, ensure_ascii=False) + "\n")
            last_user = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
            )
            if (
                last_user == "RUN_TOOL"
                and not (self.workspace / "tool_started").exists()
            ):
                response = LLMResponse(
                    content="",
                    calls=(ToolCall("read", "read_file", '{"path":"input.txt"}'),),
                )
            else:
                response = LLMResponse(content="正常回复", calls=())
        return LLMCallResult(response, LLMUsage(input_tokens=0, output_tokens=5))


def config_for(workspace: Path):
    return AssistantConfig(
        model_name="compact-test",
        workspace_root=workspace,
        full_access=False,
        model_context_limit=60000,
        input_budget_ratio=0.9,
        llm=CompactLlm(workspace),
        compact_threshold_ratio=0.55,
    )


def tool_config(workspace: Path):
    from helperme.assistant import worker
    from helperme.runtime import ToolBinding

    build = worker.build_assistant_assembly

    async def blocked_read(context, arguments):
        (workspace / "tool_started").touch()
        while not (workspace / "release_tool").exists():
            await asyncio.sleep(0.02)
        with (workspace / "tool_count").open("a") as file:
            file.write("executed\n")
        return {"ok": True, "text": "TOOL_EVIDENCE"}

    async def assembly(*args, **kwargs):
        result = await build(*args, **kwargs)
        result.runtime.bind_tool("read_file", ToolBinding(blocked_read))
        return result

    worker.build_assistant_assembly = assembly
    return config_for(workspace)
