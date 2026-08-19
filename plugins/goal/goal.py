from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from plugins.goal.verification import GoalVerification


class GoalStatus(str, Enum):
    ACTIVE = "active"
    JUDGING = "judging"
    PAUSED = "paused"
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"


class CriterionAuthority(str, Enum):
    USER = "user"
    INFERRED = "inferred"


class JudgmentDecision(str, Enum):
    DONE = "done"
    CONTINUE = "continue"
    PAUSE = "pause"


class GoalTurnStatus(str, Enum):
    EXECUTING = "executing"
    JUDGING = "judging"
    FINISHED = "finished"


@dataclass(frozen=True)
class CompletionCriterion:
    id: str
    description: str
    authority: CriterionAuthority
    evidence_requirements: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("criterion id cannot be empty")
        if not self.description.strip():
            raise ValueError("criterion description cannot be empty")
        if not self.evidence_requirements:
            raise ValueError("criterion must declare evidence requirements")
        if any(not item.strip() for item in self.evidence_requirements):
            raise ValueError("criterion evidence requirement cannot be empty")


@dataclass(frozen=True)
class CompletionContractDraft:
    criteria: tuple[CompletionCriterion, ...]
    verification: GoalVerification = GoalVerification()

    def __post_init__(self) -> None:
        if not self.criteria:
            raise ValueError("completion contract must contain criteria")
        ids = [criterion.id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("completion criterion ids must be unique")
        if not any(
            criterion.authority is CriterionAuthority.USER
            for criterion in self.criteria
        ):
            raise ValueError("completion contract must preserve a user criterion")


@dataclass(frozen=True)
class CompletionContract:
    version: int
    criteria: tuple[CompletionCriterion, ...]
    verification: GoalVerification = GoalVerification()

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("completion contract version must be positive")
        CompletionContractDraft(self.criteria, self.verification)

    @classmethod
    def initial(cls, draft: CompletionContractDraft) -> CompletionContract:
        return cls(1, draft.criteria, draft.verification)

    def revise(self, draft: CompletionContractDraft) -> CompletionContract:
        current_user = {
            criterion.id: criterion
            for criterion in self.criteria
            if criterion.authority is CriterionAuthority.USER
        }
        replacement_user = {
            criterion.id: criterion
            for criterion in draft.criteria
            if criterion.authority is CriterionAuthority.USER
        }
        if replacement_user != current_user:
            raise ValueError("user criteria cannot be added, removed, or weakened")
        return CompletionContract(
            self.version + 1,
            draft.criteria,
            draft.verification,
        )


@dataclass(frozen=True)
class ContractRevision:
    previous_version: int
    contract: CompletionContract
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("contract revision reason cannot be empty")
        if self.contract.version != self.previous_version + 1:
            raise ValueError("contract revision version must increase by one")


@dataclass(frozen=True)
class GoalJudgment:
    turn_index: int
    judge_turn_id: str
    decision: JudgmentDecision
    reason: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.turn_index < 1:
            raise ValueError("judgment turn_index must be positive")
        if not self.judge_turn_id.strip():
            raise ValueError("judge_turn_id cannot be empty")
        if not self.reason.strip():
            raise ValueError("judgment reason cannot be empty")
        if any(not item.strip() for item in self.evidence):
            raise ValueError("judgment evidence cannot be empty")
        if self.decision is JudgmentDecision.DONE and not self.evidence:
            raise ValueError("done judgment must cite evidence")


@dataclass(frozen=True)
class GoalTurn:
    index: int
    executor_turn_id: str
    contract_version: int
    status: GoalTurnStatus = GoalTurnStatus.EXECUTING
    judge_turn_id: str | None = None
    executor_answer: str | None = None


class Goal:
    def __init__(
        self,
        goal_id: str,
        objective: str,
        contract: CompletionContract,
        max_turns: int,
    ) -> None:
        if not goal_id.strip():
            raise ValueError("goal_id cannot be empty")
        if not objective.strip():
            raise ValueError("objective cannot be empty")
        if max_turns < 1:
            raise ValueError("max_turns must be positive")

        self.id = goal_id
        self.objective = objective
        self.max_turns = max_turns
        self._contract = contract
        self._contract_revisions: list[ContractRevision] = []
        self._status = GoalStatus.ACTIVE
        self._turns: list[GoalTurn] = []
        self._judgments: list[GoalJudgment] = []
        self._pause_reason: str | None = None
        self._resume_status: GoalStatus | None = None

    @property
    def status(self) -> GoalStatus:
        return self._status

    @property
    def contract(self) -> CompletionContract:
        return self._contract

    @property
    def contract_revisions(self) -> tuple[ContractRevision, ...]:
        return tuple(self._contract_revisions)

    @property
    def turns(self) -> tuple[GoalTurn, ...]:
        return tuple(self._turns)

    @property
    def judgments(self) -> tuple[GoalJudgment, ...]:
        return tuple(self._judgments)

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    @property
    def latest_feedback(self) -> str | None:
        return self._judgments[-1].reason if self._judgments else None

    def start_turn(self, executor_turn_id: str) -> GoalTurn:
        if self._status is not GoalStatus.ACTIVE:
            raise ValueError("only an active goal can start an executor turn")
        if self.turn_count >= self.max_turns:
            raise ValueError("goal has exhausted max_turns")
        if not executor_turn_id.strip():
            raise ValueError("executor_turn_id cannot be empty")
        if any(turn.executor_turn_id == executor_turn_id for turn in self._turns):
            raise ValueError(f"duplicate executor_turn_id: {executor_turn_id}")

        turn = GoalTurn(
            index=self.turn_count + 1,
            executor_turn_id=executor_turn_id,
            contract_version=self._contract.version,
        )
        self._turns.append(turn)
        return turn

    def begin_judgment(
        self,
        executor_turn_id: str,
        judge_turn_id: str,
        executor_answer: str,
    ) -> None:
        turn = self._current_turn(executor_turn_id)
        if turn.status is not GoalTurnStatus.EXECUTING:
            raise ValueError("executor turn is not executing")
        if not judge_turn_id.strip():
            raise ValueError("judge_turn_id cannot be empty")
        if not executor_answer.strip():
            raise ValueError("executor_answer cannot be empty")
        self._turns[-1] = replace(
            turn,
            status=GoalTurnStatus.JUDGING,
            judge_turn_id=judge_turn_id,
            executor_answer=executor_answer,
        )
        self._status = GoalStatus.JUDGING

    def interrupt_turn(self, executor_turn_id: str, reason: str) -> None:
        turn = self._current_turn(executor_turn_id)
        if turn.status is not GoalTurnStatus.EXECUTING:
            raise ValueError("only an executing turn can be interrupted")
        # max_turns 只统计完成到 Judge 边界的完整 Executor Turn。
        # 中断 Turn 的事实由 SessionTurnRecord 保存，不复制进 Goal 历史。
        self._turns.pop()
        self._status = GoalStatus.PAUSED
        self._resume_status = GoalStatus.ACTIVE
        self._pause_reason = reason

    def pause_judgment(self, reason: str) -> None:
        if self._status is not GoalStatus.JUDGING:
            raise ValueError("goal is not awaiting a judgment")
        self._status = GoalStatus.PAUSED
        self._resume_status = GoalStatus.JUDGING
        self._pause_reason = reason

    def revise_contract(
        self,
        draft: CompletionContractDraft,
        reason: str,
    ) -> ContractRevision:
        if self._status is not GoalStatus.JUDGING:
            raise ValueError("contract can only be revised at a judgment boundary")
        replacement = self._contract.revise(draft)
        revision = ContractRevision(self._contract.version, replacement, reason)
        self._contract = replacement
        self._contract_revisions.append(revision)
        return revision

    def record_judgment(self, judgment: GoalJudgment) -> None:
        if self._status is not GoalStatus.JUDGING:
            raise ValueError("goal is not awaiting a judgment")
        turn = self._turns[-1]
        if turn.status is not GoalTurnStatus.JUDGING:
            raise ValueError("goal turn is not awaiting a judgment")
        if judgment.turn_index != turn.index:
            raise ValueError("judgment targets the wrong turn")
        if judgment.judge_turn_id != turn.judge_turn_id:
            raise ValueError("judgment targets the wrong judge turn")

        self._judgments.append(judgment)
        self._turns[-1] = replace(turn, status=GoalTurnStatus.FINISHED)
        self._pause_reason = None
        self._resume_status = None
        if judgment.decision is JudgmentDecision.DONE:
            self._status = GoalStatus.COMPLETED
        elif judgment.decision is JudgmentDecision.PAUSE:
            self._status = GoalStatus.PAUSED
            self._resume_status = GoalStatus.ACTIVE
            self._pause_reason = judgment.reason
        elif self.turn_count >= self.max_turns:
            self._status = GoalStatus.EXHAUSTED
        else:
            self._status = GoalStatus.ACTIVE

    def resume(self) -> None:
        if self._status is not GoalStatus.PAUSED:
            raise ValueError("only a paused goal can be resumed")
        self._status = self._resume_status
        self._resume_status = None
        self._pause_reason = None

    def _current_turn(self, executor_turn_id: str) -> GoalTurn:
        if not self._turns:
            raise ValueError("goal has no turn")
        turn = self._turns[-1]
        if turn.executor_turn_id != executor_turn_id:
            raise ValueError("executor_turn_id does not match current turn")
        return turn
