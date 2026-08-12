import unittest

from core.model_call.types import ToolCall
from core.runtime_modes.plain import PlainMode
from core.tools_runtime.run_evidence import RunEvidenceRecorder
from core.tools_runtime.tool_batch import SerialToolBatchExecutor
from core.tools_runtime.tools_state import ToolsState
from tests.core.llm_test_support import runtime_tool_dependencies


class SerialToolBatchExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_executes_and_commits_results_in_source_order(self):
        dependencies = runtime_tool_dependencies()
        tools_executor = dependencies["tools_executor"]
        execution_order: list[str] = []

        async def execute(name, arguments):
            execution_order.append(name)
            return {
                "ok": True,
                "code": name.upper(),
                "data": {"arguments": arguments},
                "error": None,
                "hint": None,
            }

        tools_executor.execute = execute
        tools_state = ToolsState()
        evidence = RunEvidenceRecorder()
        calls = (
            ToolCall("call-1", "first", '{"value": 1}'),
            ToolCall("call-2", "second", '{"value": 2}'),
        )

        outcome = await SerialToolBatchExecutor(
            dependencies["tool_result_externalizer"]
        ).execute(
            calls=calls,
            tools_executor=tools_executor,
            runtime_mode=PlainMode(),
            mode_state=None,
            tools_state=tools_state,
            evidence_recorder=evidence,
        )

        self.assertEqual(execution_order, ["first", "second"])
        self.assertEqual(
            [message["tool_call_id"] for message in outcome.messages],
            ["call-1", "call-2"],
        )
        self.assertEqual(
            [step.call_id for step in evidence.snapshot().steps],
            ["call-1", "call-2"],
        )
        self.assertEqual(tools_state.summary(), {
            "total": 2,
            "pending": 0,
            "failed": 0,
        })


if __name__ == "__main__":
    unittest.main()
