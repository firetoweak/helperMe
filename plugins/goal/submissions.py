from __future__ import annotations

from dataclasses import dataclass

from plugins.goal.goal import (
    CompletionContractDraft,
    JudgmentDecision,
)


@dataclass
class ContractCompilationBuffer:
    contract: CompletionContractDraft | None = None

    def submit(self, contract: CompletionContractDraft) -> None:
        if self.contract is not None:
            raise ValueError("completion contract already submitted")
        self.contract = contract


@dataclass(frozen=True)
class JudgmentSubmission:
    decision: JudgmentDecision
    reason: str
    evidence: tuple[str, ...]
    contract_revision: CompletionContractDraft | None = None
    revision_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("judgment reason cannot be empty")
        if any(not item.strip() for item in self.evidence):
            raise ValueError("judgment evidence cannot be empty")
        if self.decision is JudgmentDecision.DONE and not self.evidence:
            raise ValueError("done judgment must cite evidence")
        if (self.contract_revision is None) != (self.revision_reason is None):
            raise ValueError(
                "contract_revision and revision_reason must be provided together"
            )
        if self.revision_reason is not None and not self.revision_reason.strip():
            raise ValueError("revision_reason cannot be empty")
        if (
            self.decision is JudgmentDecision.DONE
            and self.contract_revision is not None
        ):
            raise ValueError("done judgment cannot revise the contract")


@dataclass
class JudgmentBuffer:
    submission: JudgmentSubmission | None = None

    def submit(self, submission: JudgmentSubmission) -> None:
        self.submission = submission
