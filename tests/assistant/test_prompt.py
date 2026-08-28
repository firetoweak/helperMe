from __future__ import annotations

import unittest

from helperme.assistant.context.prompt import DEFAULT_ASSISTANT_PROMPT


class DefaultAssistantPromptTests(unittest.TestCase):
    def test_guides_workspace_verification_after_file_writes(self):
        self.assertIn(
            "使用文件写入工具修改文件后，在最终回答前调用 get_changes "
            "核对实际工作区变化；若结果表明证据不完整，使用 read_file 等读取工具"
            "补全后再作答。",
            DEFAULT_ASSISTANT_PROMPT,
        )
