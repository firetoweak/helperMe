"""Assistant 对话中的 Host 控制面，不进入 Runtime Command。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

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


@dataclass(frozen=True, slots=True)
class ControlNotice:
    message: str


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


class AssistantControlPlane:
    """在已提交 Step 之后执行提案，在用户确认后执行控制操作。"""

    def __init__(
        self,
        operations: Sequence[ControlOperation],
    ) -> None:
        self._operations = {operation.name: operation for operation in operations}
        if len(self._operations) != len(operations):
            raise ValueError("对话控制工具名称重复")
        actions = {operation.action for operation in operations}
        if len(actions) != len(operations):
            raise ValueError("控制审批 action 重复")
        self._approval_operations = {
            operation.action: operation for operation in operations
        }
        self._staged: dict[str, _StagedCall] = {}
        self._pending: dict[str, ControlApprovalRequest] = {}
        self._active_sessions: set[str] = set()

    def schemas(
        self,
        session_id: str,
        allowed_names: frozenset[str] | None = None,
    ) -> list[dict[str, object]]:
        if (
            session_id in self._active_sessions
            or session_id in self._pending
            or session_id in self._staged
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
    ) -> ControlNotice | None:
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
        finally:
            self._active_sessions.remove(session_id)
        if isinstance(result, ControlApprovalRequest):
            if result.action != staged.operation.action:
                raise ValueError(
                    f"控制 proposal action 不匹配: {result.action!r} != "
                    f"{staged.operation.action!r}"
                )
            self._pending[session_id] = result
            return ControlNotice(self._approval_message(result))
        if type(result) is not dict:
            raise TypeError("控制工具返回值不符合契约")
        return ControlNotice(json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
        ))

    def pending_view(self, session_id: str) -> ControlApprovalView | None:
        request = self._pending.get(session_id)
        if request is None:
            return None
        return ControlApprovalView(
            request.id,
            request.summary,
            request.risk,
        )

    async def resolve(self, session_id: str, *, approved: bool) -> str:
        request = self._pending.get(session_id)
        if request is None:
            raise ValueError("当前 Session 没有待确认的控制操作")
        if not approved:
            del self._pending[session_id]
            return f"已取消控制操作：{request.action}"
        del self._pending[session_id]
        operation = self._approval_operations[request.action]
        execution = await operation.approval_handler.execute(
            request.payload,
        )
        return execution.message

    @staticmethod
    def _approval_message(request: ControlApprovalRequest) -> str:
        return (
            f"{request.summary}\n"
            f"风险：{request.risk}\n"
            "输入 yes 确认，no 取消。"
        )
