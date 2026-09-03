from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from helperme.assistant.assembly import build_assistant_assembly
from helperme.assistant.context.prompt import DEFAULT_ASSISTANT_PROMPT
from helperme.assistant.management import LOAD_MANAGEMENT_TOOLS
from helperme.config import AssistantConfig
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall
from helperme.paths import HelperMeHome
from helperme.runtime import MemoryJournal, StepCommitted
from tests.benchmarks.final_session_stress_live import build_stress_assistant
from tests.live.test_runtime_live import build_live_assistant
from tests.session_scheduler import settle_session


class CapturingLlm:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def chat(self, messages, model, *, tools=None):
        self.requests.append(
            {
                "projector": "model-context/v1",
                "model": model,
                "messages": deepcopy(messages),
                "tools": deepcopy(tools),
            }
        )
        calls = ()
        if len(self.requests) == 1:
            calls = (
                ToolCall(
                    "load-management",
                    LOAD_MANAGEMENT_TOOLS,
                    '{"domain":"mcp"}',
                ),
            )
        return LLMCallResult(
            LLMResponse(content="done", calls=calls),
            LLMUsage(input_tokens=1, output_tokens=1),
        )


class AssistantAssemblyContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_entries_share_one_model_request_contract(self):
        factories = (
            build_assistant_assembly,
            build_live_assistant,
            build_stress_assistant,
        )
        requests: list[dict[str, object]] = []

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            home = HelperMeHome(root / ".helperme")
            with (
                patch(
                    "helperme.assistant.assembly.HelperMeHome.default",
                    return_value=home,
                ),
                patch(
                    "helperme.assistant.assembly.runtime_data_root",
                    return_value=root / "runtime",
                ),
            ):
                for index, factory in enumerate(factories):
                    llm = CapturingLlm()
                    journal = MemoryJournal()
                    config = AssistantConfig(
                        model_name="test-model",
                        workspace_root=workspace,
                        full_access=False,
                        model_context_limit=200_000,
                        input_budget_ratio=0.75,
                        llm=llm,
                    )
                    assembly = await factory(
                        config,
                        lambda _session_id, _text: None,
                        journal,
                    )
                    session_id = f"entry-{index}"
                    try:
                        decision = assembly.runtime.step_runner._decision_maker
                        await assembly.runtime.receive_user_message(
                            session_id,
                            "检查管理能力",
                            delivery_id="user-1",
                        )
                        state = await assembly.runtime.state(session_id)
                        allowed_control = decision._management.control_names(
                            session_id,
                            state,
                        )
                        expected_tools = [
                            *assembly.surface.schemas(session_id, state),
                            *decision._skill_tools.schemas(),
                            *decision._management.schemas(session_id, state),
                            *assembly.control.schemas(session_id, allowed_control),
                            *assembly.subagents.schemas(session_id),
                        ]
                        expected_prompt = (
                            f"{DEFAULT_ASSISTANT_PROMPT}\n\n"
                            f"{assembly.surface.catalog_instruction(session_id, state)}"
                            f"\n\n{decision._management.catalog_instruction(session_id, state)}"
                        )

                        first = await assembly.runtime.advance(session_id)
                        await settle_session(
                            assembly.runtime,
                            session_id,
                            control=assembly.control,
                        )

                        request = llm.requests[0]
                        self.assertEqual(request["tools"], expected_tools)
                        self.assertEqual(
                            request["messages"][0],
                            {"role": "system", "content": expected_prompt},
                        )
                        self.assertEqual(
                            [command.effect.name for command in first.step.commands],
                            [LOAD_MANAGEMENT_TOOLS, "deliver"],
                        )
                        event = next(
                            event
                            for event in await journal.snapshot(session_id)
                            if isinstance(event.payload, StepCommitted)
                            and event.payload.step.step_id == first.step.step_id
                        )
                        artifact_id = event.artifact_refs[0]
                        manifest = json.loads(
                            decision._projector.gateway.for_session(session_id)
                            .read(artifact_id, 0, 1_000_000)
                            .content
                        )
                        self.assertEqual(manifest["request"], request)
                        requests.append(request)

                        names = assembly.control.names()
                        self.assertTrue(names.isdisjoint(assembly.bindings))
                        self.assertTrue(names.issubset(assembly.surface._reserved))
                    finally:
                        await assembly.scheduler.close()

        self.assertEqual(requests[1:], requests[:1] * 2)


class AssemblyWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_session_endings_are_wired_to_the_subagent_host(self):
        """静止、失败、对外输出三条线都要落到 SubAgentHost。

        漏接 on_failed，子 Session 撞上模型失败就既不静止也不回收，父会拿着
        一个永远清不空的待回收集合一直等下去。
        """

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            home = HelperMeHome(root / ".helperme")
            delivered: list[tuple[str, str]] = []
            with (
                patch(
                    "helperme.assistant.assembly.HelperMeHome.default",
                    return_value=home,
                ),
                patch(
                    "helperme.assistant.assembly.runtime_data_root",
                    return_value=root / "runtime",
                ),
            ):
                assembly = await build_assistant_assembly(
                    AssistantConfig(
                        model_name="test-model",
                        workspace_root=workspace,
                        full_access=False,
                        model_context_limit=200_000,
                        input_budget_ratio=0.75,
                        llm=CapturingLlm(),
                    ),
                    lambda session_id, text: delivered.append((session_id, text)),
                    MemoryJournal(),
                )
                try:
                    scheduler = assembly.scheduler
                    self.assertEqual(
                        scheduler._on_quiesced,
                        assembly.subagents.on_quiesced,
                    )
                    self.assertEqual(
                        scheduler._on_failed,
                        assembly.subagents.on_failed,
                    )

                    assembly.subagents._parents["parent/sub-1"] = "parent"
                    await scheduler._emit("parent/sub-1", "运行失败：上游 500")
                    await scheduler._emit("parent", "父转述后的判断")

                    self.assertEqual(delivered, [("parent", "父转述后的判断")])
                finally:
                    await assembly.scheduler.close()
