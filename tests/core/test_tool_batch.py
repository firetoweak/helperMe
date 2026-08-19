import asyncio
import unittest

from pydantic import BaseModel

from core.model_call.types import ToolCall
from core.runtime_modes.plain import PlainMode
from core.tool_registry import PydanticParameters, ToolSpec
from core.tools_runtime.turn_evidence import TurnEvidenceRecorder
from core.tools_runtime.tool_batch import ConcurrentToolBatchExecutor
from core.tools_runtime.tools_state import ToolsState
from tests.core.llm_test_support import runtime_tool_dependencies


class BatchInput(BaseModel):
    value: str = ""


class ConcurrentToolBatchExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_exclusive_tool_rejects_entire_mixed_batch_before_handlers(self):
        dependencies = runtime_tool_dependencies()
        executions: list[str] = []

        async def exclusive(_input):
            executions.append("exclusive")
            return {"ok": True, "code": "LOADED"}

        async def ordinary(_input):
            executions.append("ordinary")
            return {"ok": True, "code": "READ"}

        dependencies["tools_executor"].registry.register(ToolSpec(
            name="load_skill",
            description="load",
            parameters=PydanticParameters(BatchInput),
            handler=exclusive,
            exclusive_batch=True,
        ))
        dependencies["tools_executor"].registry.register(ToolSpec(
            name="read_file_for_batch_test",
            description="read",
            parameters=PydanticParameters(BatchInput),
            handler=ordinary,
        ))
        tools_state = ToolsState()
        evidence = TurnEvidenceRecorder()

        outcome = await ConcurrentToolBatchExecutor(
            dependencies["tool_result_externalizer"]
        ).execute(
            calls=(
                ToolCall("call-1", "load_skill", '{}'),
                ToolCall("call-2", "read_file_for_batch_test", '{}'),
            ),
            tools_executor=dependencies["tools_executor"],
            runtime_mode=PlainMode(),
            mode_state=None,
            tools_state=tools_state,
            evidence_recorder=evidence,
        )

        self.assertEqual(executions, [])
        self.assertEqual(
            [step.result["code"] for step in evidence.snapshot().steps],
            [
                "EXCLUSIVE_TOOL_REQUIRES_EXCLUSIVE_BATCH",
                "EXCLUSIVE_TOOL_REQUIRES_EXCLUSIVE_BATCH",
            ],
        )
        self.assertEqual([step.ok for step in outcome.steps], [False, False])
        self.assertEqual(tools_state.summary(), {
            "total": 2,
            "pending": 0,
            "failed": 2,
        })

    async def test_two_exclusive_calls_reject_entire_batch(self):
        dependencies = runtime_tool_dependencies()
        executions: list[str] = []

        async def exclusive(_input):
            executions.append("exclusive")
            return {"ok": True, "code": "LOADED"}

        dependencies["tools_executor"].registry.register(ToolSpec(
            name="load_skill",
            description="load",
            parameters=PydanticParameters(BatchInput),
            handler=exclusive,
            exclusive_batch=True,
        ))
        evidence = TurnEvidenceRecorder()

        await ConcurrentToolBatchExecutor(
            dependencies["tool_result_externalizer"]
        ).execute(
            calls=(
                ToolCall("call-1", "load_skill", '{"value":"one"}'),
                ToolCall("call-2", "load_skill", '{"value":"two"}'),
            ),
            tools_executor=dependencies["tools_executor"],
            runtime_mode=PlainMode(),
            mode_state=None,
            tools_state=ToolsState(),
            evidence_recorder=evidence,
        )

        self.assertEqual(executions, [])
        self.assertEqual(
            [step.result["code"] for step in evidence.snapshot().steps],
            [
                "EXCLUSIVE_TOOL_REQUIRES_EXCLUSIVE_BATCH",
                "EXCLUSIVE_TOOL_REQUIRES_EXCLUSIVE_BATCH",
            ],
        )

    async def test_single_exclusive_tool_executes_normally(self):
        dependencies = runtime_tool_dependencies()
        executions: list[str] = []

        async def exclusive(input_data):
            executions.append(input_data.value)
            return {"ok": True, "code": "LOADED"}

        dependencies["tools_executor"].registry.register(ToolSpec(
            name="load_skill",
            description="load",
            parameters=PydanticParameters(BatchInput),
            handler=exclusive,
            exclusive_batch=True,
        ))
        evidence = TurnEvidenceRecorder()

        await ConcurrentToolBatchExecutor(
            dependencies["tool_result_externalizer"]
        ).execute(
            calls=(ToolCall(
                "call-1",
                "load_skill",
                '{"value":"pdf"}',
            ),),
            tools_executor=dependencies["tools_executor"],
            runtime_mode=PlainMode(),
            mode_state=None,
            tools_state=ToolsState(),
            evidence_recorder=evidence,
        )

        self.assertEqual(executions, ["pdf"])
        self.assertEqual(evidence.snapshot().steps[0].result["code"], "LOADED")

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
        evidence = TurnEvidenceRecorder()
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
        self.assertEqual(
            evidence.snapshot().steps[0].origin.tool_call_id,
            "call-1",
        )
        self.assertEqual(
            evidence.snapshot().steps[0].origin.tool_name,
            "first",
        )
        self.assertEqual(tools_state.summary(), {
            "total": 2,
            "pending": 0,
            "failed": 0,
        })

    async def test_environment_baseline_keeps_location_and_root_membership(self):
        recorder = TurnEvidenceRecorder()
        recorder.record_environment_baseline("project", {
            "ok": True,
            "code": "CHANGES_READ",
            "data": {
                "location": {
                    "environment_id": "local-test",
                    "path": "file:///repo",
                },
            },
            "error": None,
            "hint": None,
        })

        baseline = recorder.snapshot().environment_baseline("project")

        self.assertEqual(baseline.location.environment_id, "local-test")
        self.assertEqual(baseline.location.path, "file:///repo")

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
        evidence = TurnEvidenceRecorder()
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
                evidence_recorder=TurnEvidenceRecorder(),
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
                evidence_recorder=TurnEvidenceRecorder(),
            )

        self.assertTrue(sibling_finished)


if __name__ == "__main__":
    unittest.main()
