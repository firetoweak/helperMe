from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from core.tool_registry import PydanticParameters, ToolSpec
from core.tools_runtime.turn_evidence import TurnEvidence
from plugins.goal.submissions import (
    ContractCompilationBuffer,
    JudgmentBuffer,
    JudgmentSubmission,
)
from plugins.goal.goal import (
    CompletionContract,
    CompletionContractDraft,
    CompletionCriterion,
    CriterionAuthority,
    JudgmentDecision,
)
from plugins.goal.verification import (
    CommandRequirement,
    CompletionGate,
    GoalVerification,
    WorkspaceRequirement,
)


SUBMIT_COMPLETION_CONTRACT = "submit_completion_contract"
SUBMIT_GOAL_JUDGMENT = "submit_goal_judgment"


class CompletionCriterionInput(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    authority: CriterionAuthority
    evidence_requirements: list[str] = Field(min_length=1)


class CommandRequirementInput(BaseModel):
    command_contains: str = Field(min_length=1)
    workspace_root_id: str | None = None
    cwd: str | None = None
    expected_exit_codes: list[int] | None = Field(default_factory=lambda: [0])


class WorkspaceRequirementInput(BaseModel):
    root_id: str = Field(min_length=1)
    changed: bool | None = None
    allowed_paths: list[str] = Field(default_factory=list)


class GoalVerificationInput(BaseModel):
    commands: list[CommandRequirementInput] = Field(default_factory=list)
    workspace: WorkspaceRequirementInput | None = None


class CompletionContractInput(BaseModel):
    criteria: list[CompletionCriterionInput] = Field(min_length=1)
    verification: GoalVerificationInput = Field(
        default_factory=GoalVerificationInput
    )


class GoalJudgmentInput(BaseModel):
    decision: JudgmentDecision
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    revised_contract: CompletionContractInput | None = None
    revision_reason: str | None = None


def _to_contract(data: CompletionContractInput) -> CompletionContractDraft:
    workspace = data.verification.workspace
    return CompletionContractDraft(
        criteria=tuple(
            CompletionCriterion(
                id=criterion.id,
                description=criterion.description,
                authority=criterion.authority,
                evidence_requirements=tuple(criterion.evidence_requirements),
            )
            for criterion in data.criteria
        ),
        verification=GoalVerification(
            commands=tuple(
                CommandRequirement(
                    command_contains=requirement.command_contains,
                    workspace_root_id=requirement.workspace_root_id,
                    cwd=requirement.cwd,
                    expected_exit_codes=(
                        tuple(requirement.expected_exit_codes)
                        if requirement.expected_exit_codes is not None
                        else None
                    ),
                )
                for requirement in data.verification.commands
            ),
            workspace=(
                WorkspaceRequirement(
                    root_id=workspace.root_id,
                    changed=workspace.changed,
                    allowed_paths=tuple(workspace.allowed_paths),
                )
                if workspace is not None
                else None
            ),
        ),
    )


def _format_contract(contract: CompletionContract) -> str:
    criteria = [
        f"- [{item.authority.value}] {item.id}: {item.description}\n"
        f"  证据要求：{list(item.evidence_requirements)}"
        for item in contract.criteria
    ]
    commands = [
        f"- 命令包含 {item.command_contains!r}; "
        f"workspace_root_id={item.workspace_root_id!r}; "
        f"cwd={item.cwd!r}; exit_codes={item.expected_exit_codes!r}"
        for item in contract.verification.commands
    ]
    workspace = contract.verification.workspace
    workspace_line = (
        "无"
        if workspace is None
        else (
            f"root_id={workspace.root_id!r}; changed={workspace.changed!r}; "
            f"allowed_paths={list(workspace.allowed_paths)!r}"
        )
    )
    return "\n".join(
        [
            f"Completion Contract v{contract.version}",
            "完成标准：",
            *criteria,
            "结构化命令验证：",
            *(commands or ["- 无"]),
            f"Workspace 验证：{workspace_line}",
        ]
    )


def create_submit_completion_contract_spec(
    buffer: ContractCompilationBuffer,
) -> ToolSpec:
    async def submit(data: CompletionContractInput) -> dict:
        try:
            buffer.submit(_to_contract(data))
        except ValueError as exc:
            return {
                "ok": False,
                "code": "INVALID_COMPLETION_CONTRACT",
                "error": str(exc),
                "hint": "修正完成标准或证据要求后重新提交。",
            }
        return {
            "ok": True,
            "code": "COMPLETION_CONTRACT_BUFFERED",
            "data": None,
            "error": None,
            "hint": "Contract 已冻结，请直接结束本次编译 Turn。",
        }

    return ToolSpec(
        name=SUBMIT_COMPLETION_CONTRACT,
        description="根据用户 Goal 提交可验收的 Completion Contract。",
        parameters=PydanticParameters(CompletionContractInput),
        handler=submit,
    )


def create_submit_goal_judgment_spec(
    contract: CompletionContract,
    buffer: JudgmentBuffer,
) -> ToolSpec:
    async def submit(data: GoalJudgmentInput) -> dict:
        try:
            revision = (
                _to_contract(data.revised_contract)
                if data.revised_contract is not None
                else None
            )
            if revision is not None:
                contract.revise(revision)
            buffer.submit(
                JudgmentSubmission(
                    decision=data.decision,
                    reason=data.reason,
                    evidence=tuple(data.evidence),
                    contract_revision=revision,
                    revision_reason=data.revision_reason,
                )
            )
        except ValueError as exc:
            return {
                "ok": False,
                "code": "INVALID_GOAL_JUDGMENT",
                "error": str(exc),
                "hint": "修正 Judgment 或 Contract Revision 后重新提交。",
            }
        return {
            "ok": True,
            "code": "GOAL_JUDGMENT_BUFFERED",
            "data": {"decision": data.decision.value},
            "error": None,
            "hint": "请直接结束 Judge Turn。",
        }

    return ToolSpec(
        name=SUBMIT_GOAL_JUDGMENT,
        description=(
            "提交 Goal 级验收结论。done 必须建立在实际验证和充分证据上；"
            "证据不足返回 continue；用户标准客观上无法满足时返回 pause。"
        ),
        parameters=PydanticParameters(GoalJudgmentInput),
        handler=submit,
    )


@dataclass(frozen=True)
class ContractCompilationCapability:
    objective: str
    buffer: ContractCompilationBuffer

    def base_tool_names(self) -> tuple[str, ...] | None:
        return ()

    def evidence_roots(self) -> tuple[str, ...]:
        return ()

    def runtime_instructions(self) -> list[str]:
        return [
            "\n".join(
                [
                    "你是 Completion Contract 编译器，不负责规划执行步骤。",
                    f"用户 Goal：{self.objective}",
                    "把用户明确要求标记为 user；合理补充的验收标准标记为 inferred。",
                    "每条标准必须声明 Judge 需要看到的具体证据。",
                    "仅在目标明确要求时添加固定命令或路径，禁止臆造项目结构。",
                    "结束前必须调用 submit_completion_contract。",
                ]
            )
        ]

    def tool_specs(self) -> list[ToolSpec]:
        return [create_submit_completion_contract_spec(self.buffer)]

    def check_final_candidate(self, evidence: TurnEvidence) -> str | None:
        if self.buffer.contract is None:
            return "结束前必须提交 Completion Contract。"
        return None

    def checkpoint_data(self) -> dict | None:
        return {"goal_command": {"kind": "completion_contract"}}


@dataclass(frozen=True)
class GoalExecutorCapability:
    goal_id: str
    objective: str
    turn_index: int
    contract: CompletionContract
    judge_feedback: str | None = None

    def base_tool_names(self) -> tuple[str, ...] | None:
        return None

    def evidence_roots(self) -> tuple[str, ...]:
        return ()

    def runtime_instructions(self) -> list[str]:
        feedback = self.judge_feedback or "这是第一次执行，暂无 Judge 反馈。"
        return [
            "\n".join(
                [
                    f"你正在执行 Goal {self.goal_id} 的第 {self.turn_index} 个完整 Agent Turn。",
                    f"Goal：{self.objective}",
                    _format_contract(self.contract),
                    f"上一次 Judge Turn 的反馈：{feedback}",
                    "Contract 在本 Turn 内冻结。你不能降低、删除或改写完成标准。",
                    "自主决定本 Turn 的行动；TodoList 只作为 Turn 内柔性参考。",
                    "完成本 Turn 能推进的工作后直接给出真实总结，不要自行宣布 Goal 已验收完成。",
                ]
            )
        ]

    def tool_specs(self) -> list[ToolSpec]:
        return []

    def check_final_candidate(self, evidence: TurnEvidence) -> str | None:
        return None

    def checkpoint_data(self) -> dict | None:
        return {
            "goal_turn": {
                "goal_id": self.goal_id,
                "turn_index": self.turn_index,
                "contract_version": self.contract.version,
            }
        }


@dataclass(frozen=True)
class GoalJudgeCapability:
    goal_id: str
    objective: str
    turn_index: int
    contract: CompletionContract
    executor_answer: str
    buffer: JudgmentBuffer
    completion_gate: CompletionGate = field(default_factory=CompletionGate)

    def base_tool_names(self) -> tuple[str, ...] | None:
        return (
            "glob",
            "grep",
            "read_file",
            "read_artifact",
            "get_changes",
            "execute_command",
        )

    def evidence_roots(self) -> tuple[str, ...]:
        workspace = self.contract.verification.workspace
        return (workspace.root_id,) if workspace is not None else ()

    def runtime_instructions(self) -> list[str]:
        return [
            "\n".join(
                [
                    "你是独立 Goal Judge。你不继承 Executor 对话，也不负责修复。",
                    f"Goal：{self.objective}",
                    _format_contract(self.contract),
                    f"Executor 本 Turn 总结：{self.executor_answer}",
                    "不要相信 Executor 的完成声明；读取真实状态并主动执行必要验证。",
                    "done 表示全部标准已经由当前真实世界状态和证据满足。",
                    "证据不足或仍可继续推进时返回 continue，并给出下一 Turn 的具体缺口。",
                    "用户标准客观不可满足时返回 pause。",
                    "你可以修订 inferred 标准和验证方法，但不能改变任何 user 标准；"
                    "修订只从下一 Executor Turn 生效，因此不能与 done 同时提交。",
                    "结束前必须调用 submit_goal_judgment。",
                ]
            )
        ]

    def tool_specs(self) -> list[ToolSpec]:
        return [
            create_submit_goal_judgment_spec(self.contract, self.buffer)
        ]

    def check_final_candidate(self, evidence: TurnEvidence) -> str | None:
        submission = self.buffer.submission
        if submission is None:
            return "结束 Judge Turn 前必须提交 GoalJudgment。"
        if submission.decision is JudgmentDecision.DONE:
            review = self.completion_gate.review(
                self.contract.verification,
                evidence,
            )
            if not review.accepted:
                return (
                    f"done 未通过结构化证据门禁：{review.reason} "
                    "请执行缺失验证后重新提交，或改为 continue。"
                )
        return None

    def checkpoint_data(self) -> dict | None:
        submission = self.buffer.submission
        if submission is None:
            return None
        return {
            "goal_judgment": {
                "goal_id": self.goal_id,
                "turn_index": self.turn_index,
                "decision": submission.decision.value,
            }
        }
