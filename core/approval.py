from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from types import MappingProxyType


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _freeze(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    action: str
    payload: Mapping[str, Any]
    summary: str
    risk: str

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("approval id 不能为空")
        if not self.action.strip():
            raise ValueError("approval action 不能为空")
        if not self.summary.strip():
            raise ValueError("approval summary 不能为空")
        if not self.risk.strip():
            raise ValueError("approval risk 不能为空")
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True)
class ApprovalExecution:
    succeeded: bool
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("approval execution message 不能为空")
        object.__setattr__(self, "data", dict(self.data))


@dataclass(frozen=True)
class ApprovalResolution:
    approval_id: str
    decision: str
    execution: ApprovalExecution | None = None

    def __post_init__(self) -> None:
        if self.decision not in {"approved", "rejected"}:
            raise ValueError(f"unsupported approval decision: {self.decision}")
        if self.decision == "rejected" and self.execution is not None:
            raise ValueError("rejected approval 不能包含 execution")
        if self.decision == "approved" and self.execution is None:
            raise ValueError("approved approval 必须包含 execution")


class ApprovalActionHandler(Protocol):
    @property
    def action(self) -> str:
        ...

    async def execute(self, payload: Mapping[str, Any]) -> ApprovalExecution:
        ...


class ApprovalActionRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ApprovalActionHandler] = {}

    def register(self, handler: ApprovalActionHandler) -> None:
        if handler.action in self._handlers:
            raise ValueError(f"duplicate approval action: {handler.action}")
        self._handlers[handler.action] = handler

    async def execute(
        self,
        request: ApprovalRequest,
    ) -> ApprovalExecution:
        handler = self._handlers.get(request.action)
        if handler is None:
            raise KeyError(f"approval action 未注册: {request.action}")
        return await handler.execute(request.payload)
