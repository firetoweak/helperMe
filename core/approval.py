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
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True)
class ApprovalExecution:
    succeeded: bool
    message: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", dict(self.data))


@dataclass(frozen=True)
class ApprovalResolution:
    approval_id: str
    decision: str
    execution: ApprovalExecution | None = None


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
        self._handlers[handler.action] = handler

    async def execute(
        self,
        request: ApprovalRequest,
    ) -> ApprovalExecution:
        handler = self._handlers[request.action]
        return await handler.execute(request.payload)
