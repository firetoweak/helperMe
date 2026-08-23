from __future__ import annotations

import unittest

from helperme.config import load_app_config
from helperme.llm.client import LLMClient


class LlmLiveClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_roundtrip(self):
        config = load_app_config()
        async with LLMClient(config.model) as client:
            result = await client.chat(
                [{"role": "user", "content": "Reply with the single digit 2."}],
                config.model.name,
            )
        self.assertTrue(
            result.response.content.strip() or result.response.calls
        )
        self.assertGreaterEqual(result.usage.total_tokens, 1)
