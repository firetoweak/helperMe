import asyncio
import unittest

from core.model_call.types import ToolCall
from core.runtime_modes.plain import PlainMode
from core.tools_runtime.run_evidence import RunEvidenceRecorder
from core.tools_runtime.tool_batch import ConcurrentToolBatchExecutor
from core.tools_runtime.tools_state import ToolsState
from tests.core.llm_test_support import runtime_tool_dependencies


class ConcurrentToolBatchExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_executes_concurrently_and_commits_results_in_source_order(self):
        dependencies = runtime_tool_dependencies()
        tools_executor = dependencies["tools_executor"]
        both_started = asyncio.Event()
        release = asyncio.Event()
        started: set[str] = set()
        completion_order: list[str] = []

        async def execute(name, arguments):
            started.add(name)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            if name == "first":
                await asyncio.sleep(0.01)
            completion_order.append(name)
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

        batch_task = asyncio.create_task(
            ConcurrentToolBatchExecutor(
                dependencies["tool_result_externalizer"]
            ).execute(
                calls=calls,
                tools_executor=tools_executor,
                runtime_mode=PlainMode(),
                mode_state=None,
                tools_state=tools_state,
                evidence_recorder=evidence,
            )
        )
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release.set()
        outcome = await batch_task

        self.assertEqual(completion_order, ["second", "first"])
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

    async def test_structured_failure_does_not_cancel_sibling_calls(self):
        dependencies = runtime_tool_dependencies()
        tools_executor = dependencies["tools_executor"]
        completed: list[str] = []

        async def execute(name, _arguments):
            if name == "failed":
                return {
                    "ok": False,
                    "code": "EXPECTED_FAILURE",
                    "data": None,
                    "error": "failed",
                    "hint": None,
                }
            await asyncio.sleep(0)
            completed.append(name)
            return {
                "ok": True,
                "code": "OK",
                "data": None,
                "error": None,
                "hint": None,
            }

        tools_executor.execute = execute
        tools_state = ToolsState()
        evidence = RunEvidenceRecorder()
        calls = (
            ToolCall("call-1", "failed", "{}"),
            ToolCall("call-2", "sibling", "{}"),
        )

        outcome = await ConcurrentToolBatchExecutor(
            dependencies["tool_result_externalizer"]
        ).execute(
            calls=calls,
            tools_executor=tools_executor,
            runtime_mode=PlainMode(),
            mode_state=None,
            tools_state=tools_state,
            evidence_recorder=evidence,
        )

        self.assertEqual(completed, ["sibling"])
        self.assertEqual([step.ok for step in outcome.steps], [False, True])
        self.assertEqual(tools_state.summary(), {
            "total": 2,
            "pending": 0,
            "failed": 1,
        })

    async def test_cancellation_propagates_to_all_running_calls(self):
        dependencies = runtime_tool_dependencies()
        tools_executor = dependencies["tools_executor"]
        both_started = asyncio.Event()
        cancelled: set[str] = set()
        started: set[str] = set()

        async def execute(name, _arguments):
            started.add(name)
            if len(started) == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(name)
                raise

        tools_executor.execute = execute
        batch_task = asyncio.create_task(
            ConcurrentToolBatchExecutor(
                dependencies["tool_result_externalizer"]
            ).execute(
                calls=(
                    ToolCall("call-1", "first", "{}"),
                    ToolCall("call-2", "second", "{}"),
                ),
                tools_executor=tools_executor,
                runtime_mode=PlainMode(),
                mode_state=None,
                tools_state=ToolsState(),
                evidence_recorder=RunEvidenceRecorder(),
            )
        )
        await asyncio.wait_for(both_started.wait(), timeout=1)
        batch_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await batch_task

        self.assertEqual(cancelled, {"first", "second"})

    async def test_internal_exception_waits_for_sibling_then_propagates(self):
        dependencies = runtime_tool_dependencies()
        tools_executor = dependencies["tools_executor"]
        sibling_finished = False

        async def execute(name, _arguments):
            nonlocal sibling_finished
            if name == "crashed":
                raise RuntimeError("handler bug")
            await asyncio.sleep(0.01)
            sibling_finished = True
            return {
                "ok": True,
                "code": "OK",
                "data": None,
                "error": None,
                "hint": None,
            }

        tools_executor.execute = execute

        with self.assertRaisesRegex(RuntimeError, "handler bug"):
            await ConcurrentToolBatchExecutor(
                dependencies["tool_result_externalizer"]
            ).execute(
                calls=(
                    ToolCall("call-1", "crashed", "{}"),
                    ToolCall("call-2", "sibling", "{}"),
                ),
                tools_executor=tools_executor,
                runtime_mode=PlainMode(),
                mode_state=None,
                tools_state=ToolsState(),
                evidence_recorder=RunEvidenceRecorder(),
            )

        self.assertTrue(sibling_finished)


if __name__ == "__main__":
    unittest.main()
