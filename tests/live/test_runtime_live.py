from __future__ import annotations

import asyncio
import unittest

from helperme.runtime import MemoryJournal, RuntimeStatus
from helperme.assistant.assembly import build_assistant_assembly
from tests.session_scheduler import SettlingScheduler
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
        journal = MemoryJournal()
        assembly = await build_assistant_assembly(
            config,
            delivered.append,
            journal,
            scheduler_factory=SettlingScheduler,
        )
        session_id = "live-session"
        try:
            async with config.llm, assembly.mcp.client_manager:
                await assembly.sessions.create(session_id)
                await assembly.sessions.receive_user_message(
                    session_id,
                    "只用一句话回答：1+1 等于几。不要调用工具。",
                    delivery_id="live-1",
                )
                await asyncio.wait_for(
                    assembly.scheduler.join(),
                    timeout=180,
                )
                state = await assembly.runtime.state(session_id)
        finally:
            await assembly.scheduler.close()
        events = await journal.snapshot(session_id)
        kinds = [event.payload.__class__.__name__ for event in events]
        self.assertIn("UserMessageReceived", kinds)
        self.assertIn("StepCommitted", kinds)
        self.assertTrue(delivered, kinds)
        self.assertEqual(state.status, RuntimeStatus.WAITING)
        self.assertEqual(state.waiting_for, ("user_message",))
