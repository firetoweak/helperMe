import unittest

from core.prompt import (
    DEFAULT_AGENT_PROMPT,
    ENVIRONMENT_RULE,
    PARALLEL_TOOL_CALLS_RULE,
)


class AgentPromptTest(unittest.TestCase):
    def test_parallel_tool_call_contract_is_in_default_agent_prompt(self):
        self.assertIn(PARALLEL_TOOL_CALLS_RULE, DEFAULT_AGENT_PROMPT)
        self.assertIn("同一响应中的工具调用会并发执行", DEFAULT_AGENT_PROMPT)
        self.assertIn("Runtime 不会替你推断语义依赖", DEFAULT_AGENT_PROMPT)
        self.assertIn("不要仅为增加并行度而制造额外的工具调用", DEFAULT_AGENT_PROMPT)

    def test_environment_semantics_are_stable_developer_instructions(self):
        self.assertIn(ENVIRONMENT_RULE, DEFAULT_AGENT_PROMPT)
        self.assertIn("相对路径以当前 cwd 为基准", ENVIRONMENT_RULE)
        self.assertIn("workspace_roots", ENVIRONMENT_RULE)
        self.assertIn("location", ENVIRONMENT_RULE)


if __name__ == "__main__":
    unittest.main()
