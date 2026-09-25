from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import re

from helperme.runtime import DomainFactCommitted
from helperme.runtime.model import CommandPhase
from helperme.sandbox.versions import WorkspaceRestoreFailed, WorkspaceVersions


WORKSPACE_VERSION_FACT = "assistant.workspace_version"
WORKSPACE_RESTORE_FACT = "assistant.workspace_restore"
WORKSPACE_REWIND_FACT = "assistant.workspace_rewind"
WORKSPACE_CARRYOVER_FACT = "assistant.workspace_carryover"


class WorkspaceRewindFailed(Exception):
    """人点的回退没做成。失败本身已经写进 Journal，这里只负责回话。"""


@dataclass(frozen=True)
class WorkspaceVersionFact:
    workspace_id: str
    step_id: str | None
    version: str | None
    error: str | None

    @classmethod
    def parse(cls, data) -> WorkspaceVersionFact:
        if not isinstance(data, Mapping) or set(data) != {
            "workspace_id", "step_id", "version", "error"
        }:
            raise ValueError("workspace version fact fields invalid")
        if type(data["workspace_id"]) is not str or not data["workspace_id"]:
            raise ValueError("workspace version workspace_id invalid")
        for key in ("step_id", "version", "error"):
            if data[key] is not None and (type(data[key]) is not str or not data[key]):
                raise ValueError(f"workspace version {key} invalid")
        _validate_version_result(data["version"], data["error"])
        return cls(**data)


def _validate_version_result(version, error):
    if (version is None) == (error is None):
        raise ValueError("workspace version must contain either version or error")
    if version is not None and (type(version) is not str or re.fullmatch(r"[0-9a-f]{40}", version) is None):
        raise ValueError("workspace version identity invalid")
    if error is not None and (type(error) is not str or not error):
        raise ValueError("workspace version error invalid")


def project_workspace_versions(events) -> tuple[WorkspaceVersionFact, ...]:
    return tuple(
        WorkspaceVersionFact.parse(event.payload.data)
        for event in events
        if isinstance(event.payload, DomainFactCommitted)
        and event.payload.fact_type == WORKSPACE_VERSION_FACT
    )


def project_workspace_restores(events):
    restores = {}
    for event in events:
        payload = event.payload
        if not isinstance(payload, DomainFactCommitted) or payload.fact_type != WORKSPACE_RESTORE_FACT:
            continue
        data = payload.data
        if not isinstance(data, Mapping) or set(data) != {
            "command_id", "tool_call_id", "before_version", "version", "error"
        }:
            raise ValueError("workspace restore fact fields invalid")
        for key in ("command_id", "tool_call_id", "before_version"):
            if type(data[key]) is not str or not data[key]:
                raise ValueError(f"workspace restore {key} invalid")
        if re.fullmatch(r"[0-9a-f]{40}", data["before_version"]) is None:
            raise ValueError("workspace restore rescue version invalid")
        _validate_version_result(data["version"], data["error"])
        restores[data["command_id"]] = data
    return restores


class WorkspaceVersionBoundary:
    def __init__(self, runtime, session_id: str, workspace_id: str,
                 versions: WorkspaceVersions) -> None:
        self.runtime = runtime
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.versions = versions

    async def sync(self) -> None:
        events = await self.runtime.snapshot(self.session_id)
        recorded = {fact.step_id for fact in project_workspace_versions(events)}
        state = self.runtime.projector.project_visible(self.session_id, events)
        completed = [
            step for step in state.steps
            if step.step.step_id not in recorded
            and all(command.phase is CommandPhase.TERMINAL
                    or command.authorization_rejected_by_event_id is not None
                    for command in step.commands)
        ]
        if None not in recorded:
            await self._record(None, None)
        for step in completed:
            await self._record(step.step.step_id, step.committed_event_id)

    async def _record(self, step_id: str | None, cause: str | None) -> None:
        try:
            version = await self.versions.record()
        except OSError as exc:
            fact = WorkspaceVersionFact(self.workspace_id, step_id, None, str(exc))
        else:
            fact = WorkspaceVersionFact(self.workspace_id, step_id, version, None)
        await self.runtime.receive_domain_fact(
            self.session_id, WORKSPACE_VERSION_FACT, asdict(fact),
            delivery_id=step_id or "initial", source=WORKSPACE_VERSION_FACT,
            causation_id=cause,
        )


    async def rewind(self, step_id, delivery_id):
        """人点某个 Step：退回到这一步跑完时的文件状态。

        比模型的 restore_workspace 晚一格。模型说的是「撤销这次调用」，
        取前一步的版本；人指着时间轴上的一个点说「回到这一刻」，取的就是
        这一步自己的版本。差这一格是故意的。
        """
        events = await self.runtime.snapshot(self.session_id)
        facts = {fact.step_id: fact for fact in project_workspace_versions(events)}
        fact = facts.get(step_id)
        if fact is None or fact.version is None:
            return {"ok": False, "code": "WORKSPACE_VERSION_UNAVAILABLE",
                    "error": "这一步没有成功的版本记录，无法回退。"}
        try:
            restored = await self.versions.restore(fact.version)
        except WorkspaceRestoreFailed as error:
            await self._record_rewind(delivery_id, step_id, error.before_version, None, str(error))
            return {"ok": False, "code": "WORKSPACE_RESTORE_FAILED", "error": str(error)}
        await self._record_rewind(delivery_id, step_id, restored.before_version, restored.version, None)
        return {"ok": True, "code": "WORKSPACE_REWOUND", "step_id": step_id}

    async def settle_fork(self, restore, delivery_id):
        """新分支要不要连文件一起退回分支点。

        退：回到这条分支历史里最后记下的那个状态。不退：磁盘上留着本分支
        历史看不到的改动，模型必须知道，否则它会照着读到的文件往下推。
        分支点之后本来就没动过文件时两条路没有区别，也就没什么可说的。
        """
        events = await self.runtime.snapshot(self.session_id)
        recorded = [f for f in project_workspace_versions(events) if f.version is not None]
        if not recorded:
            return
        target = recorded[-1]
        if await self.versions.record() == target.version:
            return
        if not restore:
            await self.runtime.receive_domain_fact(
                self.session_id, WORKSPACE_CARRYOVER_FACT,
                {"note": "工作区文件保持现状，没有退回这条分支的历史位置；"
                         "磁盘上存在本分支历史里看不到的改动。"},
                delivery_id=delivery_id, source=WORKSPACE_CARRYOVER_FACT,
            )
            return
        restored = await self.versions.restore(target.version)
        await self._record_rewind(
            delivery_id, target.step_id, restored.before_version, restored.version, None
        )

    async def _record_rewind(self, delivery_id, step_id, before, version, error):
        # 人的回退没有工具返回值，模型只能从这条事实知道文件被挪过。
        await self.runtime.receive_domain_fact(
            self.session_id, WORKSPACE_REWIND_FACT,
            {"step_id": step_id, "before_version": before,
             "version": version, "error": error},
            delivery_id=delivery_id, source=WORKSPACE_REWIND_FACT,
        )

    async def restore(self, command_id, target, events, visible):
        target_step = next((step for step in visible.steps
                            if any(c.command.command_id == target for c in step.commands)), None)
        if target_step is None:
            return {"ok": False, "code": "UNKNOWN_TOOL_CALL", "error": "当前上下文没有该调用。"}
        all_steps = self.runtime.projector.project_visible(self.session_id, events).steps
        position = next(i for i, step in enumerate(all_steps)
                        if step.step.step_id == target_step.step.step_id)
        previous_id = None if position == 0 else all_steps[position - 1].step.step_id
        facts = {fact.step_id: fact for fact in project_workspace_versions(events)}
        previous = facts.get(previous_id)
        if previous is None or previous.version is None:
            return {"ok": False, "code": "WORKSPACE_VERSION_UNAVAILABLE",
                    "error": "该调用前一步没有成功的版本记录，无法回退。"}
        version = previous.version
        # 回退调用自身有精确的执行前救援快照，包含两步之间的手工修改。
        restore = project_workspace_restores(events).get(target)
        if restore is not None:
            version = restore["before_version"]
        try:
            restored = await self.versions.restore(version)
        except WorkspaceRestoreFailed as error:
            await self._record_restore(command_id, target, error.before_version, None, str(error))
            return {"ok": False, "code": "WORKSPACE_RESTORE_FAILED", "error": str(error),
                    "hint": "恢复前的文件已保存，可引用本次回退调用 id 撤销这次恢复。"}
        except OSError as error:
            return {"ok": False, "code": "WORKSPACE_RESTORE_FAILED", "error": str(error)}
        await self._record_restore(command_id, target, restored.before_version, restored.version, None)
        return {"ok": True, "code": "WORKSPACE_RESTORED", "tool_call_id": target}

    async def _record_restore(self, command_id, target, before, version, error):
        await self.runtime.receive_domain_fact(
            self.session_id, WORKSPACE_RESTORE_FACT,
            {"command_id": command_id, "tool_call_id": target, "before_version": before,
             "version": version, "error": error},
            delivery_id=command_id, source=WORKSPACE_RESTORE_FACT,
        )
