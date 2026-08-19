from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.context import ContextState
from core.tools_runtime.turn_evidence import TurnEvidence
from core.tools_runtime.tools_checkpoint import Checkpoint
from core.approval import ApprovalRequest


class TurnStatus(str, Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class TurnControl:
    interrupt_requested: bool = False
    interrupt_reason: str | None = None

    def request_interrupt(self, reason: str | None = None) -> None:
        self.interrupt_requested = True
        self.interrupt_reason = reason


@dataclass
class TurnResult:
    status: TurnStatus
    answer: str
    checkpoints: list[Checkpoint]
    context_state: ContextState
    evidence: TurnEvidence
    approval_request: ApprovalRequest | None = None

    @property
    def final_reason(self) -> str | None:
        if self.status == TurnStatus.COMPLETED or not self.checkpoints:
            return None
        return self.checkpoints[-1].reason
