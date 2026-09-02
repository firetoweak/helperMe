from __future__ import annotations

import asyncio
import unittest

from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.assistant.subagent import (
    DELEGATE,
    READONLY_TOOL_NAMES,
    REPORT,
    REPORT_FACT,
    TASK_FACT,
    SubAgentHost,
    project_delegations,
    project_parent,
    project_reclaimed,
    project_report,
)
from helperme.llm.api import LLMProviderError
from helperme.runtime import (
    AgentRuntime,
    DomainFactCommitted,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    RuntimeStatus,
)
from helperme.runtime.dispatcher import AttemptContext
from helperme.runtime.state import DecisionFrame
from tests.assistant.test_runner import SequentialIds
from tests.session_scheduler import SettlingScheduler


def _attempt_context(session_id: str) -> AttemptContext:
    return AttemptContext(session_id, "command-x", "attempt-x", 1)


def _facts(events, fact_type: str):
    return [
        event.payload
        for event in events
        if isinstance(event.payload, DomainFactCommitted)
        and event.payload.fact_type == fact_type
    ]


class _Interleaving:
    """让 snapshot 真正交出控制权。

    脚本模型不会阻塞，两个子 Session 就总是一个跑完再跑另一个，测不出并发
    回收下的读改写。真实模型每次决策都要等网络，交错是常态。
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    async def snapshot(self, session_id: str):
        await asyncio.sleep(0)
        return await self._inner.snapshot(session_id)

    async def receive_domain_fact(self, *args, **kwargs):
        await asyncio.sleep(0)
        return await self._inner.receive_domain_fact(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ParentChildDecisions:
    """父与子跑各自的脚本。子 Session 由 id 里的 /sub- 认出。"""

    def __init__(self, parent_scripts, child_scripts) -> None:
        self._parent_scripts = parent_scripts
        self._child_scripts = child_scripts
        self.parent_frames: list[DecisionFrame] = []
        self.child_frames: list[DecisionFrame] = []
        self._by_child: dict[str, list[DecisionFrame]] = {}

    async def decide(self, frame: DecisionFrame) -> ModelDecision:
        session_id = frame.trigger_event.session_id
        if "/sub-" in session_id:
            # 每个子 Session 走自己的进度，并行委派时互不串位。
            own = self._by_child.setdefault(session_id, [])
            script = self._child_scripts[len(own)]
            own.append(frame)
            self.child_frames.append(frame)
        else:
            script = self._parent_scripts[len(self.parent_frames)]
            self.parent_frames.append(frame)
        return script(frame)


class SubAgentDelegationTest(unittest.IsolatedAsyncioTestCase):
    PARENT = "parent-session"

    def _build(
        self,
        parent_scripts,
        child_scripts,
        *,
        delivered=None,
        interleave=False,
    ):
        host = SubAgentHost()
        model = _ParentChildDecisions(parent_scripts, child_scripts)
        bindings = dict(host.bindings())
        if delivered is not None:
            bindings.update(
                deliver_binding(
                    host.routed_sink(
                        lambda session_id, text: delivered.append(
                            (session_id, text)
                        )
                    )
                )
            )
        runtime = AgentRuntime(
            MemoryJournal(),
            model,
            bindings,
            SequentialIds(),
        )
        scheduler = SettlingScheduler(
            runtime,
            notify=(
                host.routed_sink(
                    lambda session_id, text: delivered.append((session_id, text))
                )
                if delivered is not None
                else None
            ),
            on_quiesced=host.on_quiesced,
            on_failed=host.on_failed,
        )
        host.attach(_Interleaving(runtime) if interleave else runtime, scheduler)
        return host, model, runtime, scheduler

    async def _child_session_id(self, runtime) -> str:
        reports = _facts(await runtime.snapshot(self.PARENT), REPORT_FACT)
        return reports[0].data["child_session_id"]

    async def test_single_delegation_returns_its_report_to_the_parent(self):
        delivered: list[tuple[str, str]] = []
        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查清 A 的调用点"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="收到子 Agent 结论"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(
                            REPORT,
                            (("summary", "A 有三个调用点，见 x.py:10"),),
                        ),
                    ),
                ),
            ),
            delivered=delivered,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "帮我查 A",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            parent_events = await runtime.snapshot(self.PARENT)
            reports = _facts(parent_events, REPORT_FACT)
            self.assertEqual(len(reports), 1)
            self.assertIs(reports[0].requests_decision, True)
            self.assertEqual(
                reports[0].data["summary"],
                "A 有三个调用点，见 x.py:10",
            )
            self.assertIs(reports[0].data["reported"], True)

            child_session_id = reports[0].data["child_session_id"]
            self.assertTrue(host.is_subagent(child_session_id))
            self.assertEqual(host.parent_of(child_session_id), self.PARENT)

            # 子的任务事实落在子自己的 Journal，父只拿到结论。
            child_events = await runtime.snapshot(child_session_id)
            tasks = _facts(child_events, TASK_FACT)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].data["task"], "查清 A 的调用点")
            self.assertEqual(_facts(parent_events, TASK_FACT), [])

            # 父因为这条事实醒来，做了第二次决策。
            self.assertEqual(len(model.parent_frames), 2)
            self.assertEqual(
                model.parent_frames[1].trigger_event.payload.fact_type,
                REPORT_FACT,
            )
            self.assertEqual(
                (await runtime.state(self.PARENT)).status,
                RuntimeStatus.WAITING,
            )
        finally:
            await scheduler.close()

    async def test_one_step_can_open_several_children_at_once(self):
        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    content="我同时让两个子 Agent 去查",
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查 A"),)),
                        InvokeTool(DELEGATE, (("task", "查 B"),)),
                    ),
                ),
                # 两条结论各自到达还是挤在一次决策里取决于时序，
                # 父可能被唤醒一次也可能两次，脚本给足即可。
                lambda _frame: ModelDecision(content="收到一条"),
                lambda _frame: ModelDecision(content="收到另一条"),
            ),
            child_scripts=(
                lambda frame: ModelDecision(
                    command_requests=(
                        InvokeTool(
                            REPORT,
                            (
                                (
                                    "summary",
                                    f"结论来自 {frame.trigger_event.session_id}",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            interleave=True,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "同时查 A 和 B",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            parent_events = await runtime.snapshot(self.PARENT)
            reports = _facts(parent_events, REPORT_FACT)
            children = [report.data["child_session_id"] for report in reports]

            self.assertEqual(len(reports), 2)
            self.assertEqual(len(set(children)), 2)
            for report in reports:
                self.assertIs(report.data["reported"], True)
                self.assertEqual(
                    report.data["summary"],
                    f"结论来自 {report.data['child_session_id']}",
                )
            # 两条结论都送到了，父不会漏看其中一条。
            self.assertEqual(
                set(project_delegations(parent_events)),
                set(children),
            )
            self.assertEqual(project_reclaimed(parent_events), frozenset(children))
            # 先回来的那条说明还差一个，父据此知道结论不齐；最后一条归零。
            self.assertEqual(
                [report.data["pending_children"] for report in reports],
                [1, 0],
            )
        finally:
            await scheduler.close()

    async def test_failing_child_is_reclaimed_as_a_failure_not_as_silence(self):
        def _boom(_frame):
            raise LLMProviderError("上游返回 500")

        delivered: list[tuple[str, str]] = []
        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查清 A"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="子 Agent 没跑完，我换个做法"),
            ),
            child_scripts=(_boom,),
            delivered=delivered,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(self.PARENT, "查 A", delivery_id="user-1")

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            parent_events = await runtime.snapshot(self.PARENT)
            reports = _facts(parent_events, REPORT_FACT)

            self.assertEqual(len(reports), 1)
            report = reports[0]
            # 失败不被改写成「停机但无产出」，父能看出区别。
            self.assertIs(report.data["reported"], False)
            self.assertIsNone(report.data["summary"])
            self.assertIn("上游返回 500", report.data["failure"])
            # 失败也是终局：父不会拿着一个永不归零的计数干等。
            self.assertEqual(report.data["pending_children"], 0)
            self.assertEqual(
                project_reclaimed(parent_events),
                frozenset(project_delegations(parent_events)),
            )
            # 父被叫醒并做了下一步判断。
            self.assertEqual(len(model.parent_frames), 2)
            # 子那条裸错误没有冒到用户面前，用户该看到的是父的转述。
            self.assertEqual(delivered, [])
        finally:
            await scheduler.close()

    async def test_parent_failure_still_reaches_the_user(self):
        def _boom(_frame):
            raise LLMProviderError("上游返回 500")

        delivered: list[tuple[str, str]] = []
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(_boom,),
            child_scripts=(),
            delivered=delivered,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(self.PARENT, "查 A", delivery_id="user-1")

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            # 拦的是子 Session 的对外输出，不是所有失败。
            self.assertEqual(len(delivered), 1)
            session_id, text = delivered[0]
            self.assertEqual(session_id, self.PARENT)
            self.assertIn("上游返回 500", text)
            # 父没有父，回收无处可去。
            self.assertEqual(_facts(await runtime.snapshot(self.PARENT), REPORT_FACT), [])
        finally:
            await scheduler.close()

    async def test_child_stopping_without_report_is_reclaimed_honestly(self):
        host, model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "看一眼 B"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="子 Agent 没有产出"),
            ),
            child_scripts=(lambda _frame: ModelDecision(content="我看完了"),),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "看 B",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()

            reports = _facts(await runtime.snapshot(self.PARENT), REPORT_FACT)
            self.assertEqual(len(reports), 1)
            self.assertIs(reports[0].data["reported"], False)
            self.assertIsNone(reports[0].data["summary"])
        finally:
            await scheduler.close()

    async def test_subagent_output_is_not_routed_anywhere(self):
        delivered: list[tuple[str, str]] = []
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查 C"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="好"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(REPORT, (("summary", "C 没有问题"),)),
                    ),
                ),
            ),
            delivered=delivered,
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 C",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
            child_session_id = await self._child_session_id(runtime)

            routed = host.routed_sink(
                lambda session_id, text: delivered.append((session_id, text))
            )
            await routed(child_session_id, "子 Agent 的中间过程")
            await routed(self.PARENT, "给用户的结论")

            self.assertEqual(delivered, [(self.PARENT, "给用户的结论")])
        finally:
            await scheduler.close()

    async def test_child_cannot_delegate_again(self):
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查 D"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="好"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(REPORT, (("summary", "D 正常"),)),
                    ),
                ),
            ),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 D",
            delivery_id="user-1",
        )

        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
            child_session_id = await self._child_session_id(runtime)

            self.assertNotIn(DELEGATE, READONLY_TOOL_NAMES)
            self.assertEqual(
                [
                    schema["function"]["name"]
                    for schema in host.schemas(child_session_id)
                ],
                [REPORT],
            )
            refused = await host._delegate(
                _attempt_context(child_session_id),
                {"task": "再委派一层"},
            )
            self.assertIs(refused["ok"], False)
            self.assertEqual(refused["code"], "DELEGATION_NOT_ALLOWED")
        finally:
            await scheduler.close()


    async def test_children_are_recognised_again_after_restart(self):
        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查 E"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="好"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(REPORT, (("summary", "E 正常"),)),
                    ),
                ),
            ),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 E",
            delivery_id="user-1",
        )
        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
        finally:
            await scheduler.close()
        child_session_id = await self._child_session_id(runtime)
        events = await runtime.snapshot(self.PARENT)

        restarted = SubAgentHost()
        restarted_scheduler = SettlingScheduler(
            runtime,
            on_quiesced=restarted.on_quiesced,
        )
        restarted.attach(runtime, restarted_scheduler)
        try:
            self.assertFalse(restarted.is_subagent(child_session_id))
            pending = await restarted.rehydrate(self.PARENT)

            # 认回来是安全边界：不认回，子 Session 就没有只读限制了。
            self.assertTrue(restarted.is_subagent(child_session_id))
            self.assertEqual(
                restarted.tool_names(child_session_id),
                READONLY_TOOL_NAMES,
            )
            # 已经回收过，不需要再推它。
            self.assertEqual(pending, ())
            self.assertEqual(project_delegations(events), (child_session_id,))
            self.assertEqual(
                project_reclaimed(events),
                frozenset({child_session_id}),
            )

            # 回收事实还没写下来时，同一个子就是待推进的那个。
            before_reclaim = tuple(
                event
                for event in events
                if not (
                    isinstance(event.payload, DomainFactCommitted)
                    and event.payload.fact_type == REPORT_FACT
                )
            )
            self.assertEqual(project_reclaimed(before_reclaim), frozenset())
            self.assertEqual(
                project_delegations(before_reclaim),
                (child_session_id,),
            )
        finally:
            await restarted_scheduler.close()

    async def test_child_recognises_itself_without_going_through_its_parent(self):
        """直接恢复一个子 Session 时，只读边界不能因为没经过父而丢失。"""

        host, _model, runtime, scheduler = self._build(
            parent_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELEGATE, (("task", "查 F"),)),
                    ),
                ),
                lambda _frame: ModelDecision(content="好"),
            ),
            child_scripts=(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(REPORT, (("summary", "F 正常"),)),
                    ),
                ),
            ),
        )
        await runtime.create_session(self.PARENT)
        await runtime.receive_user_message(
            self.PARENT,
            "查 F",
            delivery_id="user-1",
        )
        try:
            await scheduler.wake(self.PARENT)
            await scheduler.join()
        finally:
            await scheduler.close()
        child_session_id = await self._child_session_id(runtime)

        restarted = SubAgentHost()
        restarted_scheduler = SettlingScheduler(
            runtime,
            on_quiesced=restarted.on_quiesced,
        )
        restarted.attach(runtime, restarted_scheduler)
        try:
            pending = await restarted.rehydrate(child_session_id)

            self.assertEqual(pending, ())
            self.assertTrue(restarted.is_subagent(child_session_id))
            self.assertEqual(
                restarted.parent_of(child_session_id),
                self.PARENT,
            )
            self.assertEqual(
                project_parent(await runtime.snapshot(self.PARENT)),
                None,
            )
        finally:
            await restarted_scheduler.close()


class SubAgentPolicyTest(unittest.IsolatedAsyncioTestCase):
    def test_readonly_names_exclude_every_writing_tool(self):
        for name in (
            "write_file",
            "apply_patch",
            "replace_all",
            "execute_command",
            DELEGATE,
        ):
            with self.subTest(tool=name):
                self.assertNotIn(name, READONLY_TOOL_NAMES)

    def test_unknown_session_has_no_policy(self):
        host = SubAgentHost()
        self.assertIsNone(host.tool_names("plain-session"))
        self.assertIsNone(host.system_prompt("plain-session"))
        self.assertEqual(
            [schema["function"]["name"] for schema in host.schemas("plain")],
            [DELEGATE],
        )

    def test_session_without_any_report_projects_to_none(self):
        self.assertIsNone(project_report(()))
