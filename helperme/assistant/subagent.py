"""SubAgent 委派与回收。

子 Session 是普通独立 Session：同一套 create / advance / recover，自己的
Journal 与判定。父子关系只存在于 Assistant 侧，用因果事实表达，Runtime 不
增加 `parent_session_id` 或 `agent_type`。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from helperme.assistant.context.prompt import SUBAGENT_PROMPT
from helperme.assistant.delivery import DeliverySink, emit_delivery
from helperme.runtime import (
    AgentRuntime,
    CommandOutcomeReceived,
    DomainFactCommitted,
    Event,
    InvokeTool,
    OutcomeStatus,
    StepCommitted,
    ToolBinding,
)
from helperme.runtime.dispatcher import AttemptContext
from helperme.runtime.model import CanonicalState


DELEGATE = "delegate"
REPORT = "report"

TASK_FACT = "subagent.task"
REPORT_FACT = "subagent.report"

FACT_SOURCE = "subagent"


READONLY_TOOL_NAMES = frozenset(
    {
        "glob",
        "grep",
        "read_file",
        "get_changes",
        "read_artifact",
        "load_skill",
        "read_skill_resource",
        REPORT,
    }
)
"""子 Session 能看见的全部工具。

显式列举而不是排除写工具：新增任何工具默认进不来，要进必须有人明确加。
`execute_command` 永远不在其中——一条命令是否只读无法静态判断。`DELEGATE`
也不在其中，递归委派因此被同一份名单挡住。
"""


DELEGATE_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": DELEGATE,
        "description": (
            "把一件可独立完成的只读调查任务委派给子 Agent。"
            "子 Agent 有自己的上下文，过程不会占用当前对话；"
            "它只能读取，不能修改工作区或执行命令。"
            "本次调用只返回“已创建”，结论稍后作为一条事实送回。"
            "多件互不依赖的任务可以在同一次决策里各发一次 delegate，"
            "子 Agent 之间并行推进。"
            "结论一条条回来，每条带 pending_children 表示还有几个子 Agent 没回；"
            "它不为 0 说明结论还没齐，此时不要给出最终答复。"
            "回来的也可能是失败：failure 非空表示该子 Agent 没能跑完，"
            "字段里是失败原因，由你判断重派、换做法还是如实告诉用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "交给子 Agent 的完整任务描述。它看不到当前对话，"
                        "所需背景必须写在这里。"
                    ),
                },
            },
            "required": ["task"],
        },
    },
}


REPORT_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": REPORT,
        "description": (
            "把结论交回父 Agent。这是唯一会被父 Agent 看到的通道。"
            "summary 要能独立读懂：结论、依据、以及没能确定的部分。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "面向父 Agent 的完整结论，含依据与遗留问题。",
                },
            },
            "required": ["summary"],
        },
    },
}


def project_delegations(events: Sequence[Event]) -> tuple[str, ...]:
    """从父 Session 的 Journal 重建它创建过的子 Session。"""

    delegate_commands: set[str] = set()
    for event in events:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            continue
        for command in payload.step.commands:
            effect = command.effect
            if isinstance(effect, InvokeTool) and effect.name == DELEGATE:
                delegate_commands.add(command.command_id)

    children: list[str] = []
    for event in events:
        payload = event.payload
        if (
            not isinstance(payload, CommandOutcomeReceived)
            or payload.command_id not in delegate_commands
            or payload.outcome.status is not OutcomeStatus.SUCCEEDED
        ):
            continue
        value = payload.outcome.value
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            continue
        data = value.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("delegate outcome data 无效")
        child_session_id = data.get("child_session_id")
        if type(child_session_id) is not str or not child_session_id:
            raise ValueError("delegate outcome child_session_id 无效")
        children.append(child_session_id)
    return tuple(children)


def project_parent(events: Sequence[Event]) -> str | None:
    """从一条 Session 自己的 Journal 判断它是不是子 Session。"""

    for event in events:
        payload = event.payload
        if (
            not isinstance(payload, DomainFactCommitted)
            or payload.fact_type != TASK_FACT
        ):
            continue
        data = payload.data
        if not isinstance(data, Mapping):
            raise ValueError("subagent 任务事实 data 无效")
        parent_session_id = data.get("parent_session_id")
        if type(parent_session_id) is not str or not parent_session_id:
            raise ValueError("subagent 任务事实 parent_session_id 无效")
        return parent_session_id
    return None


def project_reclaimed(events: Sequence[Event]) -> frozenset[str]:
    """父 Session 已经收到过结论的那些子 Session。"""

    reclaimed: set[str] = set()
    for event in events:
        payload = event.payload
        if (
            not isinstance(payload, DomainFactCommitted)
            or payload.fact_type != REPORT_FACT
        ):
            continue
        data = payload.data
        if not isinstance(data, Mapping):
            raise ValueError("subagent 回收事实 data 无效")
        reclaimed.add(data["child_session_id"])
    return frozenset(reclaimed)


def project_report(events: Sequence[Event]) -> str | None:
    """从子 Session 的 Journal 取回最后一次成功的 report 内容。"""

    report_commands: set[str] = set()
    for event in events:
        payload = event.payload
        if not isinstance(payload, StepCommitted):
            continue
        for command in payload.step.commands:
            effect = command.effect
            if isinstance(effect, InvokeTool) and effect.name == REPORT:
                report_commands.add(command.command_id)

    summary: str | None = None
    for event in events:
        payload = event.payload
        if (
            not isinstance(payload, CommandOutcomeReceived)
            or payload.command_id not in report_commands
            or payload.outcome.status is not OutcomeStatus.SUCCEEDED
        ):
            continue
        value = payload.outcome.value
        if not isinstance(value, Mapping):
            raise ValueError("report outcome 必须是 object")
        data = value.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("report outcome data 无效")
        reported = data.get("summary")
        if type(reported) is not str or not reported:
            raise ValueError("report outcome summary 无效")
        summary = reported
    return summary


class SubAgentHost:
    """委派、回收，以及子 Session 的策略边界。"""

    def __init__(self) -> None:
        self._runtime: AgentRuntime | None = None
        self._scheduler = None
        self._parents: dict[str, str] = {}
        self._reclaim_locks: dict[str, asyncio.Lock] = {}

    def attach(self, runtime: AgentRuntime, scheduler) -> None:
        self._runtime = runtime
        self._scheduler = scheduler

    def is_subagent(self, session_id: str) -> bool:
        return session_id in self._parents

    def parent_of(self, session_id: str) -> str | None:
        return self._parents.get(session_id)

    def tool_names(self, session_id: str) -> frozenset[str] | None:
        """本 Session 允许出现的工具名；None 表示不设限。"""

        return READONLY_TOOL_NAMES if self.is_subagent(session_id) else None

    def system_prompt(self, session_id: str) -> str | None:
        return SUBAGENT_PROMPT if self.is_subagent(session_id) else None

    def schemas(self, session_id: str) -> list[dict[str, object]]:
        if self.is_subagent(session_id):
            return [REPORT_SCHEMA]
        return [DELEGATE_SCHEMA]

    def bindings(self) -> dict[str, ToolBinding]:
        return {
            DELEGATE: ToolBinding(self._delegate, decision_on_outcome=False),
            REPORT: ToolBinding(self._report, decision_on_outcome=False),
        }

    def routed_sink(self, sink: DeliverySink) -> DeliverySink:
        """子 Session 的 deliver 没有去处，内容留在它自己的 Journal 里。"""

        async def routed(session_id: str, text: str) -> None:
            if self.is_subagent(session_id):
                return
            await emit_delivery(sink, session_id, text)

        return routed

    async def rehydrate(self, session_id: str) -> tuple[str, ...]:
        """重启后认回父子关系，并唤醒还没回收的子 Session。

        `_parents` 是进程内的可丢弃缓存。不重建它，重启后的子 Session 会
        失去只读边界，未回收的委派也再没有人推进。恢复的可能是父，也可能
        直接就是某个子，两个方向都要认得出来。
        """

        runtime = self._require_runtime()
        events = await runtime.snapshot(session_id)
        parent_session_id = project_parent(events)
        if parent_session_id is not None:
            self._parents[session_id] = parent_session_id
            return ()
        reclaimed = project_reclaimed(events)
        pending: list[str] = []
        for child_session_id in project_delegations(events):
            self._parents[child_session_id] = session_id
            if child_session_id not in reclaimed:
                pending.append(child_session_id)
        for child_session_id in pending:
            await self._require_scheduler().wake(child_session_id)
        return tuple(pending)

    async def on_quiesced(
        self,
        session_id: str,
        state: CanonicalState,
    ) -> None:
        """Session 静止时判断是否到了回收一个子 Session 的时候。"""

        parent_session_id = self._parents.get(session_id)
        if parent_session_id is None:
            return
        # 没有人会来回答，落到等人说话就等于本轮做完了。等授权或等命令都不是。
        # 允许递归委派后，这里还要加上「没有未回收的子 Session」。
        if state.waiting_for != ("user_message",):
            return
        runtime = self._require_runtime()
        summary = project_report(await runtime.snapshot(session_id))
        await self._reclaim(
            session_id,
            parent_session_id,
            summary=summary,
            failure=None,
        )

    async def on_failed(self, session_id: str, message: str) -> None:
        """子 Session 撞上已识别的失败，也是一种终局。

        失败不会让它静止，不回收父就一直等，`pending_children` 永远不归零。
        失败原文原样交给父：父是 Judge，重试还是换路由它判断，这里不改写、
        不降级成「无产出」。
        """

        parent_session_id = self._parents.get(session_id)
        if parent_session_id is None:
            return
        await self._reclaim(
            session_id,
            parent_session_id,
            summary=None,
            failure=message,
        )

    async def _reclaim(
        self,
        session_id: str,
        parent_session_id: str,
        *,
        summary: str | None,
        failure: str | None,
    ) -> None:
        """把一个子 Session 的终局交回父。一个子最多回收一次。"""

        runtime = self._require_runtime()
        # 并行委派的子会各自终局。不串行化，它们会在对方写入回收事实前读到
        # 同一份父 Journal，各自都以为还有人没回来，父就永远等不到 0。
        lock = self._reclaim_locks.setdefault(parent_session_id, asyncio.Lock())
        async with lock:
            await runtime.receive_domain_fact(
                parent_session_id,
                REPORT_FACT,
                {
                    "child_session_id": session_id,
                    "reported": summary is not None,
                    "summary": summary,
                    "failure": failure,
                    "pending_children": await self._pending_children(
                        parent_session_id,
                        session_id,
                    ),
                },
                delivery_id=f"{session_id}:report",
                source=FACT_SOURCE,
                requests_decision=True,
            )
        await self._require_scheduler().wake(parent_session_id)

    async def _pending_children(
        self,
        parent_session_id: str,
        reclaiming: str,
    ) -> int:
        """父还有几个子没交回结论，不含正在回收的这个。

        父不会等子凑齐才被唤醒，每条结论都单独叫醒它一次。少了这个数，父只
        能自己数委派过几个、收到过几条，容易在结论不全时就下判断。
        """

        events = await self._require_runtime().snapshot(parent_session_id)
        delegated = set(project_delegations(events))
        return len(delegated - project_reclaimed(events) - {reclaiming})

    async def _delegate(
        self,
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        task = arguments.get("task")
        if type(task) is not str or not task.strip():
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "data": {"task": task},
                "error": "task 必须是非空字符串",
            }
        if self.is_subagent(context.session_id):
            return {
                "ok": False,
                "code": "DELEGATION_NOT_ALLOWED",
                "data": {"session_id": context.session_id},
                "error": "子 Agent 不能再委派",
            }
        runtime = self._require_runtime()
        # 从委派命令派生，重放同一条命令不会造出第二个子 Session。
        child_session_id = f"{context.session_id}/sub-{context.command_id}"
        await runtime.create_session(child_session_id)
        self._parents[child_session_id] = context.session_id
        await runtime.receive_domain_fact(
            child_session_id,
            TASK_FACT,
            {
                "task": task.strip(),
                "parent_session_id": context.session_id,
            },
            delivery_id=f"{context.command_id}:task",
            source=FACT_SOURCE,
            requests_decision=True,
        )
        await self._require_scheduler().wake(child_session_id)
        return {
            "ok": True,
            "code": "DELEGATED",
            "data": {"child_session_id": child_session_id},
        }

    async def _report(
        self,
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> dict[str, object]:
        summary = arguments.get("summary")
        if type(summary) is not str or not summary.strip():
            return {
                "ok": False,
                "code": "INVALID_ARGUMENT",
                "data": {"summary": summary},
                "error": "summary 必须是非空字符串",
            }
        return {
            "ok": True,
            "code": "REPORTED",
            "data": {"summary": summary.strip()},
        }

    def _require_runtime(self) -> AgentRuntime:
        if self._runtime is None:
            raise RuntimeError("SubAgentHost 尚未绑定 Runtime")
        return self._runtime

    def _require_scheduler(self):
        if self._scheduler is None:
            raise RuntimeError("SubAgentHost 尚未绑定 Scheduler")
        return self._scheduler
