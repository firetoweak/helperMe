"""Assistant 对话中的 Host 控制面，不进入 Runtime Command。

待裁决提案只有一份事实，在 Journal 里：出现 `assistant.control.proposed`
而其后没有 `assistant.control.resolved`，就是待裁决。Host 与 Worker 各自重放
同一段事件得到同一个答案，进程之间不传这份状态，也不留镜像。
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Mapping, Sequence

from helperme.runtime.events import DomainFactCommitted, Event
from helperme.runtime.json_values import thaw_value
from helperme.runtime.model import Step
from helperme.runtime.state import DecisionFrame
from helperme.tools.control import (
    ControlApprovalRequest,
    ControlOperation,
)
from helperme.tools.spec import ToolArgumentsError


class ControlArgumentsError(ValueError):
    def __init__(self, details: object) -> None:
        super().__init__("control arguments validation failed")
        self.details = details


@dataclass(frozen=True, slots=True)
class ControlApprovalView:
    request_id: str
    summary: str
    risk: str


CONTROL_SOURCE = "assistant.control"
CONTROL_PROPOSED = "assistant.control.proposed"
CONTROL_CONCLUDED = "assistant.control.concluded"
CONTROL_FAILED = "assistant.control.failed"
CONTROL_RESOLVED = "assistant.control.resolved"


class NoPendingControlApproval(LookupError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"当前 Session 没有待确认的控制操作：{session_id}")


@dataclass(frozen=True, slots=True)
class ControlOutcome:
    """一次控制提案的结局：只成为事实，再决定要不要继续决策。"""

    fact_type: str
    data: Mapping[str, object]
    delivery_id: str
    requests_decision: bool


@dataclass(frozen=True, slots=True)
class ControlResolution:
    request_id: str
    action: str
    approved: bool
    succeeded: bool
    message: str
    data: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _DecisionKey:
    session_id: str
    trigger_event_id: str
    decision_cursor: int
    basis_state_version: str


@dataclass(frozen=True, slots=True)
class _StagedCall:
    key: _DecisionKey
    operation: ControlOperation
    input_data: object


def project_pending_approval(
    events: Sequence[Event],
) -> ControlApprovalRequest | None:
    """有 proposed、其后没有 resolved，就是待裁决。"""

    pending: ControlApprovalRequest | None = None
    for event in events:
        payload = event.payload
        if not isinstance(payload, DomainFactCommitted):
            continue
        if payload.fact_type == CONTROL_PROPOSED:
            pending = _request_from_fact(thaw_value(payload.data))
        elif payload.fact_type == CONTROL_RESOLVED:
            pending = None
    return pending


def pending_approval_view(events: Sequence[Event]) -> ControlApprovalView | None:
    request = project_pending_approval(events)
    if request is None:
        return None
    return ControlApprovalView(request.id, request.summary, request.risk)


def _request_from_fact(data: object) -> ControlApprovalRequest:
    # Journal 反序列化是外部边界：字段缺失或类型不符是持久化损坏，当场暴露。
    if not isinstance(data, Mapping):
        raise ValueError("控制提案事实 data 无效")
    payload = data["payload"]
    if not isinstance(payload, Mapping):
        raise ValueError("控制提案事实 payload 无效")
    return ControlApprovalRequest(
        id=data["request_id"],
        action=data["action"],
        payload=payload,
        summary=data["summary"],
        risk=data["risk"],
    )


def _step_delivery_id(step: Step, kind: str) -> str:
    return f"{step.trigger_event_id}:{step.decision_cursor}:control_{kind}"


class AssistantControlPlane:
    """在已提交 Step 之后执行提案，在用户确认后执行控制操作。"""

    def __init__(
        self,
        operations: Sequence[ControlOperation],
    ) -> None:
        self._operations = {operation.name: operation for operation in operations}
        if len(self._operations) != len(operations):
            raise ValueError("对话控制工具名称重复")
        self._approval_operations = {
            operation.action: operation for operation in operations
        }
        if len(self._approval_operations) != len(operations):
            raise ValueError("控制审批 action 重复")
        self._staged: dict[str, _StagedCall] = {}
        self._active_sessions: set[str] = set()

    def schemas(
        self,
        session_id: str,
        events: Sequence[Event],
        allowed_names: frozenset[str] | None = None,
    ) -> list[dict[str, object]]:
        if (
            session_id in self._active_sessions
            or project_pending_approval(events) is not None
        ):
            return []
        names = self.names() if allowed_names is None else allowed_names
        unknown = names.difference(self._operations)
        if unknown:
            raise ValueError(f"未知控制工具: {sorted(unknown)}")
        return [
            operation.proposal_spec.to_openai_tool()
            for name, operation in self._operations.items()
            if name in names
        ]

    def names(self) -> frozenset[str]:
        return frozenset(self._operations)

    def begin_decision(self, session_id: str) -> None:
        """丢弃上一轮未提交的暂存。

        Step 未提交时重试会复用同一个 frame，旧暂存的 key 因此与新 Step 的 key
        完全相同，`after_committed_step()` 无法靠 key 区分新旧。清理只能发生在
        本轮 stage 之前。
        """

        self._staged.pop(session_id, None)

    def stage(
        self,
        frame: DecisionFrame,
        name: str,
        arguments: Mapping[str, object],
    ) -> None:
        operation = self._operations[name]
        try:
            input_data = operation.proposal_spec.parameters.validate(
                dict(arguments)
            )
        except ToolArgumentsError as exc:
            raise ControlArgumentsError(exc.details) from exc
        key = _DecisionKey(
            frame.state.session_id,
            frame.trigger_event.event_id,
            frame.decision_cursor,
            frame.basis_state_version,
        )
        self._staged[frame.state.session_id] = _StagedCall(
            key,
            operation,
            input_data,
        )

    async def after_committed_step(
        self,
        session_id: str,
        step: Step,
    ) -> ControlOutcome | None:
        key = _DecisionKey(
            session_id,
            step.trigger_event_id,
            step.decision_cursor,
            step.basis_state_version,
        )
        staged = self._staged.pop(session_id, None)
        if staged is None or staged.key != key:
            return None
        self._active_sessions.add(session_id)
        try:
            result = await staged.operation.proposal_spec.handler(staged.input_data)
        except Exception as error:
            # 提案要去探测外部世界（网络、子进程）。做不成是一件世界事实，不是
            # 内部契约违规；完整诊断进事实，模型据此改口。
            return ControlOutcome(
                CONTROL_FAILED,
                {
                    "tool": staged.operation.name,
                    "action": staged.operation.action,
                    "error_type": (
                        f"{type(error).__module__}.{type(error).__qualname__}"
                    ),
                    "error": str(error),
                    "traceback": "".join(traceback.format_exception(error)),
                },
                _step_delivery_id(step, "failed"),
                True,
            )
        finally:
            self._active_sessions.remove(session_id)
        if isinstance(result, ControlApprovalRequest):
            if result.action != staged.operation.action:
                raise ValueError(
                    f"控制 proposal action 不匹配: {result.action!r} != "
                    f"{staged.operation.action!r}"
                )
            return ControlOutcome(
                CONTROL_PROPOSED,
                {
                    "request_id": result.id,
                    "action": result.action,
                    "payload": dict(result.payload),
                    "summary": result.summary,
                    "risk": result.risk,
                },
                f"{result.id}:proposed",
                False,
            )
        if type(result) is not dict:
            raise TypeError("控制工具返回值不符合契约")
        return ControlOutcome(
            CONTROL_CONCLUDED,
            {
                "tool": staged.operation.name,
                "action": staged.operation.action,
                "result": result,
            },
            _step_delivery_id(step, "concluded"),
            True,
        )

    async def resolve(
        self,
        request: ControlApprovalRequest,
        *,
        approved: bool,
    ) -> ControlResolution:
        if not approved:
            return ControlResolution(
                request.id,
                request.action,
                False,
                False,
                f"已取消控制操作：{request.action}",
                {},
            )
        operation = self._approval_operations[request.action]
        execution = await operation.approval_handler.execute(
            request.payload,
        )
        return ControlResolution(
            request.id,
            request.action,
            True,
            execution.succeeded,
            execution.message,
            dict(execution.data),
        )
