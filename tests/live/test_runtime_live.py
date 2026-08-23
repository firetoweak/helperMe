from __future__ import annotations

import unittest

from helperme.runtime import AgentRuntime, MemoryJournal, RuntimeStatus
from helperme.assistant.assembly import build_assistant_assembly
from helperme.assistant.decision import JournalBackedLlmDecisionMaker
from helperme.assistant.runner import drive_until_idle
from helperme.config import assistant_config_from_app, load_app_config
from helperme.llm.client import LLMClient


class RuntimeLiveModelTest(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_step_calls_real_model_and_delivers(self):
        app_config = load_app_config()
        config = assistant_config_from_app(
            app_config,
            LLMClient(app_config.model),
        )
        delivered: list[str] = []
        assembly = await build_assistant_assembly(config, delivered.append)
        journal = MemoryJournal()
        runtime = AgentRuntime(
            journal,
            JournalBackedLlmDecisionMaker(
                journal,
                config.llm,
                config.model_name,
                surface=assembly.surface,
                skill_tools=assembly.skill_tools,
                projector=assembly.projector,
            ),
            assembly.bindings,
        )
        assembly.surface.attach(runtime)
        stream_id = "live-stream"
        async with config.llm, assembly.mcp.client_manager:
            await runtime.receive_user_message(
                stream_id,
                "只用一句话回答：1+1 等于几。不要调用工具。",
                delivery_id="live-1",
            )
            result = await drive_until_idle(
                runtime,
                stream_id,
                max_steps=8,
            )
        events = await journal.snapshot(stream_id)
        kinds = [event.payload.__class__.__name__ for event in events]
        self.assertIn("UserMessageReceived", kinds)
        self.assertIn("StepCommitted", kinds)
        self.assertTrue(delivered, kinds)
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(result.state.waiting_for, ("user_message",))
