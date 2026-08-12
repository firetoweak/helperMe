from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.context import ContextState
from core.tools_runtime.run_evidence import RunEvidence
from core.tools_runtime.tools_checkpoint import Checkpoint


class RunStatus(str, Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass
class RunControl:
    interrupt_requested: bool = False
    interrupt_reason: str | None = None

    def request_interrupt(self, reason: str | None = None) -> None:
        self.interrupt_requested = True
        self.interrupt_reason = reason


@dataclass
class RunResult:
    status: RunStatus
    answer: str
    checkpoints: list[Checkpoint]
    context_state: ContextState
    evidence: RunEvidence

    @property
    def final_reason(self) -> str | None:
        if self.status == RunStatus.COMPLETED or not self.checkpoints:
            return None
        return self.checkpoints[-1].reason
