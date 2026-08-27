"""Assistant 对话中的 Host 控制面，不进入 Runtime Command。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from helperme.runtime.model import Step
from helperme.runtime.state import DecisionFrame
from helperme.tools.control import (
    ControlApprovalExecution,
    ControlApprovalRequest,
)
from helperme.tools.spec import ToolArgumentsError, ToolSpec


class ControlArgumentsError(ValueError):
    def __init__(self, details: object) -> None:
        super().__init__("control arguments validation failed")
        self.details = details


class ControlApprovalHandler(Protocol):
    action: str

    async def execute(
        self,
        payload: Mapping[str, Any],
    ) -> ControlApprovalExecution:
        ...


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
    spec: ToolSpec
    input_data: object


class AssistantControlPlane:
    """在已提交 Step 之后执行提案，在用户确认后执行控制操作。"""

    def __init__(
        self,
        specs: Sequence[ToolSpec],
        handlers: Sequence[ControlApprovalHandler],
    ) -> None:
        if any(not spec.control_boundary for spec in specs):
            raise ValueError("对话控制工具必须声明 control_boundary")
        if any(not spec.exclusive_batch for spec in specs):
            raise ValueError("对话控制工具必须声明 exclusive_batch")
        self._specs = {spec.name: spec for spec in specs}
        if len(self._specs) != len(specs):
            raise ValueError("对话控制工具名称重复")
        self._handlers = {handler.action: handler for handler in handlers}
        if len(self._handlers) != len(handlers):
            raise ValueError("控制审批 action 重复")
        self._staged: dict[_DecisionKey, _StagedCall] = {}
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
            or any(key.session_id == session_id for key in self._staged)
        ):
            return []
        names = self.names() if allowed_names is None else allowed_names
        unknown = names.difference(self._specs)
        if unknown:
            raise ValueError(f"未知控制工具: {sorted(unknown)}")
        return [
            spec.to_openai_tool()
            for name, spec in self._specs.items()
            if name in names
        ]

    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def stage(
        self,
        frame: DecisionFrame,
        name: str,
        arguments: Mapping[str, object],
    ) -> None:
        spec = self._specs[name]
        try:
            input_data = spec.parameters.validate(dict(arguments))
        except ToolArgumentsError as exc:
            raise ControlArgumentsError(exc.details) from exc
        key = _DecisionKey(
            frame.state.session_id,
            frame.trigger_event.event_id,
            frame.decision_cursor,
            frame.basis_state_version,
        )
        self._staged[key] = _StagedCall(spec, input_data)

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
        staged = self._staged.pop(key, None)
        if staged is None:
            return None
        self._active_sessions.add(session_id)
        try:
            result = await staged.spec.handler(staged.input_data)
        finally:
            self._active_sessions.remove(session_id)
        if isinstance(result, ControlApprovalRequest):
            if result.action not in self._handlers:
                raise KeyError(result.action)
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
        execution = await self._handlers[request.action].execute(
            request.payload,
        )
        del self._pending[session_id]
        return execution.message

    @staticmethod
    def _approval_message(request: ControlApprovalRequest) -> str:
        return (
            f"{request.summary}\n"
            f"风险：{request.risk}\n"
            "输入 yes 确认，no 取消。"
        )
