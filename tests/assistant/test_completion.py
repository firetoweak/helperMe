from __future__ import annotations

import json
import unittest
from collections.abc import Awaitable, Callable

from helperme.assistant.completion.criteria import (
    CriteriaCommitted,
    CriteriaSource,
    CriterionStatus,
    JudgmentCommitted,
    JudgmentVerdict,
    StreamFacts,
    classify_user_intent,
    criteria_fact,
    criteria_from_fact,
    current_criteria,
    inferred_from_facts,
    judgment_from_fact,
)
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.completion.judgment import (
    IsolatedJudge,
    JudgmentPolicy,
    ScriptedJudge,
    parse_judgment,
)
from helperme.assistant.runner import drive_until_idle
from helperme.runtime import (
    AgentRuntime,
    DomainFactCommitted,
    InvokeTool,
    LifecycleIntent,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
    ToolBinding,
)
from helperme.runtime.state import DecisionFrame
from helperme.llm.types import LLMCallResult, LLMResponse, LLMUsage, ToolCall


DecisionScript = Callable[
    [DecisionFrame],
    ModelDecision | Awaitable[ModelDecision],
]


class SequentialIds:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}-{self._value}"


class ScriptedDecisionMaker:
    def __init__(self, scripts: tuple[DecisionScript, ...]) -> None:
        self.scripts = scripts
        self.frames: list[DecisionFrame] = []

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        script = self.scripts[len(self.frames)]
        self.frames.append(frame)
        decision = script(frame)
        if isinstance(decision, Awaitable):
            return await decision
        return decision


def _payloads(events):
    return [event.payload for event in events]


class CriteriaAndJudgeTest(unittest.IsolatedAsyncioTestCase):
    STREAM_ID = "judge-stream"

    def test_matching_malformed_domain_facts_are_not_silently_ignored(self):
        malformed_criteria = DomainFactCommitted(
            "helperme.criteria.committed.v1",
            {"version": 1},
        )
        malformed_judgment = DomainFactCommitted(
            "helperme.judgment.committed.v1",
            {"verdict": "done"},
        )

        with self.assertRaisesRegex(ValueError, "criteria fact"):
            criteria_from_fact(malformed_criteria)
        with self.assertRaisesRegex(ValueError, "judgment fact"):
            judgment_from_fact(malformed_judgment)

    def test_relax_inferred_does_not_change_the_user_objective(self):
        intent = classify_user_intent(
            CriteriaCommitted(
                version=1,
                user_objective="修这个 bug",
                strict_completion=True,
                inferred=inferred_from_facts(
                    StreamFacts(True, False, None),
                ),
                source=CriteriaSource.CLASSIFIER,
            ),
            "先改完，测试一会再说",
        )
        self.assertEqual(intent.kind, "relax_inferred")
        self.assertEqual(intent.deferred_ids, ("inf-verify",))

    def test_parse_judgment_reads_json_object(self):
        self.assertEqual(
            parse_judgment('好的\n{"verdict":"done","summary":"测试过了"}'),
            (JudgmentVerdict.DONE, "测试过了"),
        )
        self.assertIsNone(parse_judgment("还不行"))

    async def test_chat_complete_without_mutation_skips_judge(self):
        delivered: list[str] = []
        judge = ScriptedJudge((
            (JudgmentVerdict.DONE, "should not run"),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="done",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "done"),)),
                    ),
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
            deliver_binding(delivered.append),
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "hello",
            delivery_id="ask-1",
        )
        policy = JudgmentPolicy(judge)
        await policy.on_user_message(runtime, self.STREAM_ID, "hello")
        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
            policy=policy,
        )
        events = await runtime.snapshot(self.STREAM_ID)
        self.assertEqual(result.state.status, RuntimeStatus.COMPLETED)
        self.assertEqual(delivered, ["done"])
        self.assertFalse(any(
            judgment_from_fact(payload) is not None
            for payload in _payloads(events)
        ))
        snapshot = current_criteria(events)
        self.assertIsNotNone(snapshot)
        self.assertFalse(snapshot.strict_completion)

    async def test_strict_judge_runs_before_commandless_completion_finalizes(self):
        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="claim done",
                lifecycle_intent=LifecycleIntent.COMPLETE,
            ),
            lambda _frame: ModelDecision(
                content="really done",
                lifecycle_intent=LifecycleIntent.COMPLETE,
            ),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {},
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "完成严格任务",
            delivery_id="strict-1",
        )
        await runtime.record_fact(
            self.STREAM_ID,
            criteria_fact(CriteriaCommitted(
                version=1,
                user_objective="完成严格任务",
                strict_completion=True,
                inferred=(),
                source=CriteriaSource.USER,
            )),
        )
        policy = JudgmentPolicy(ScriptedJudge((
            (JudgmentVerdict.CONTINUE, "还需要检查"),
            (JudgmentVerdict.DONE, "检查完成"),
        )))

        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=4,
            policy=policy,
        )
        events = await runtime.snapshot(self.STREAM_ID)
        judgments = [
            judgment
            for payload in _payloads(events)
            if (judgment := judgment_from_fact(payload)) is not None
        ]

        self.assertEqual(result.state.status, RuntimeStatus.COMPLETED)
        self.assertEqual(len(model.frames), 2)
        self.assertEqual(
            [item.verdict for item in judgments],
            [JudgmentVerdict.CONTINUE, JudgmentVerdict.DONE],
        )

    async def test_file_write_requires_judge_and_continue_reopens_a_step(self):
        delivered: list[str] = []

        async def write_file(_context, _arguments):
            return {"ok": True, "path": "a.py"}

        judge = ScriptedJudge((
            (JudgmentVerdict.CONTINUE, "还没核对测试"),
            (JudgmentVerdict.DONE, "测试已过"),
        ))
        model = ScriptedDecisionMaker((
            lambda _frame: ModelDecision(
                content="writing",
                command_requests=(
                    InvokeTool("write_file", (("path", "a.py"),)),
                ),
            ),
            lambda _frame: ModelDecision(
                content="claim done",
                command_requests=(
                    InvokeTool(DELIVER_TOOL_NAME, (("text", "claim done"),)),
                ),
                lifecycle_intent=LifecycleIntent.COMPLETE,
            ),
            lambda _frame: ModelDecision(
                content="really done",
                command_requests=(
                    InvokeTool(DELIVER_TOOL_NAME, (("text", "really done"),)),
                ),
                lifecycle_intent=LifecycleIntent.COMPLETE,
            ),
        ))
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            {
                "write_file": ToolBinding(write_file),
                **deliver_binding(delivered.append),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "改 a.py",
            delivery_id="ask-1",
        )
        policy = JudgmentPolicy(judge)
        await policy.on_user_message(runtime, self.STREAM_ID, "改 a.py")
        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
            policy=policy,
        )
        events = await runtime.snapshot(self.STREAM_ID)
        snapshot = current_criteria(events)
        judgments = [
            judgment
            for payload in _payloads(events)
            if (judgment := judgment_from_fact(payload)) is not None
        ]

        self.assertEqual(result.state.status, RuntimeStatus.COMPLETED)
        self.assertEqual(delivered, ["claim done", "really done"])
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.strict_completion)
        self.assertEqual(
            {item.criterion_id for item in snapshot.inferred},
            {"inf-workspace", "inf-verify"},
        )
        self.assertEqual(
            [item.verdict for item in judgments],
            [JudgmentVerdict.CONTINUE, JudgmentVerdict.DONE],
        )
        self.assertEqual(len(model.frames), 3)
        self.assertIsNotNone(
            judgment_from_fact(model.frames[2].trigger_event.payload),
        )

    async def test_judge_pause_keeps_stream_waiting_for_the_user(self):
        async def write_file(_context, _arguments):
            return {"ok": True}

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="writing",
                    command_requests=(InvokeTool("write_file"),),
                ),
                lambda _frame: ModelDecision(
                    content="claim done",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "claim done"),)),
                    ),
                    lifecycle_intent=LifecycleIntent.COMPLETE,
                ),
            )),
            {
                "write_file": ToolBinding(write_file),
                **deliver_binding(lambda _text: None),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "改文件",
            delivery_id="ask-1",
        )
        policy = JudgmentPolicy(
            ScriptedJudge(((JudgmentVerdict.PAUSE, "证据不够"),)),
        )
        await policy.on_user_message(runtime, self.STREAM_ID, "改文件")
        result = await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
            policy=policy,
        )
        self.assertEqual(result.state.status, RuntimeStatus.WAITING)
        self.assertEqual(result.state.waiting_for, ("user_message",))

    async def test_later_user_message_defers_inferred_without_changing_goal(self):
        async def write_file(_context, _arguments):
            return {"ok": True}

        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker((
                lambda _frame: ModelDecision(
                    content="writing",
                    command_requests=(InvokeTool("write_file"),),
                ),
                lambda _frame: ModelDecision(
                    content="working",
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("text", "working"),)),
                    ),
                ),
            )),
            {
                "write_file": ToolBinding(write_file),
                **deliver_binding(lambda _text: None),
            },
            SequentialIds(),
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "修这个 bug",
            delivery_id="ask-1",
        )
        policy = JudgmentPolicy(
            ScriptedJudge(((JudgmentVerdict.DONE, "unused"),)),
        )
        await policy.on_user_message(runtime, self.STREAM_ID, "修这个 bug")
        await drive_until_idle(
            runtime,
            self.STREAM_ID,
            max_steps=8,
            policy=policy,
        )
        await runtime.receive_user_message(
            self.STREAM_ID,
            "先改完，测试一会再说",
            delivery_id="ask-2",
        )
        await policy.on_user_message(
            runtime,
            self.STREAM_ID,
            "先改完，测试一会再说",
        )
        events = await runtime.snapshot(self.STREAM_ID)
        snapshot = current_criteria(events)
        self.assertEqual(snapshot.user_objective, "修这个 bug")
        deferred = {
            item.criterion_id: item.status
            for item in snapshot.inferred
        }
        self.assertEqual(deferred["inf-verify"], CriterionStatus.DEFERRED)
        self.assertEqual(deferred["inf-workspace"], CriterionStatus.ACTIVE)
        self.assertEqual(snapshot.source, CriteriaSource.USER_REVISION)

    async def test_isolated_judge_does_not_inherit_work_chat_or_write(self):
        chats: list[list[dict[str, object]]] = []

        class FakeLLM:
            async def chat(self, messages, model, tools=None):
                chats.append(messages)
                if len(chats) == 1:
                    return LLMCallResult(
                        LLMResponse(
                            content="",
                            calls=(
                                ToolCall(
                                    "call-1",
                                    "write_file",
                                    '{"path": "hack.py"}',
                                ),
                            ),
                        ),
                        LLMUsage(1, 1),
                    )
                return LLMCallResult(
                    LLMResponse(
                        content=json.dumps(
                            {"verdict": "pause", "summary": "只读"},
                            ensure_ascii=False,
                        ),
                    ),
                    LLMUsage(1, 1),
                )

        wrote: list[str] = []

        class Runner:
            async def execute(self, name, arguments):
                wrote.append(name)
                return {"ok": True}

        judge = IsolatedJudge(
            FakeLLM(),
            "test-model",
            [
                {
                    "type": "function",
                    "function": {"name": "write_file", "parameters": {}},
                },
                {
                    "type": "function",
                    "function": {"name": "get_changes", "parameters": {}},
                },
            ],
            Runner(),
        )
        verdict, summary = await judge(
            (),
            CriteriaCommitted(
                version=1,
                user_objective="修 bug",
                strict_completion=True,
                inferred=(),
                source=CriteriaSource.USER,
            ),
            "step-1",
        )
        self.assertEqual(verdict, JudgmentVerdict.PAUSE)
        self.assertEqual(summary, "只读")
        self.assertEqual(wrote, [])
        self.assertEqual(chats[0][0]["role"], "system")
        self.assertIn("独立 Judge", chats[0][0]["content"])
        denied = json.loads(chats[1][-1]["content"])
        self.assertFalse(denied["ok"])
