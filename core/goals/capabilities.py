from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel, Field

from core.goals.commands import GoalCommandBuffer
from core.goals.goal import (
    DependencyChange,
    Goal,
    OutcomeDecision,
    PlanRevision,
    Task,
    TaskDraft,
    TaskOutcome,
)
from core.goals.verification import (
    CommandRequirement,
    CompletionGate,
    TaskVerification,
    WorkspaceRequirement,
)
from core.tool_registry import ToolSpec
from core.tools_runtime.run_evidence import RunEvidence


SUBMIT_TASK_OUTCOME = "submit_task_outcome"
SUBMIT_PLAN_REVISION = "submit_plan_revision"


class SubmitTaskOutcomeInput(BaseModel):
    decision: OutcomeDecision
    summary: str = Field(min_length=1)
    evidence: list[str]


class CommandRequirementInput(BaseModel):
    command_contains: str = Field(
        min_length=1,
        description="成功命令字符串必须包含的稳定片段",
    )
    root: str | None = Field(
        default=None,
        description="可选的 workspace root 名称",
    )
    cwd: str | None = Field(
        default=None,
        description="可选的 root 内相对工作目录",
    )
    expected_exit_codes: list[int] | None = Field(
        default_factory=lambda: [0],
        description=(
            "允许的退出码；默认 [0] 表示必须成功，null 表示只要求命令真实完成"
        ),
    )


class WorkspaceRequirementInput(BaseModel):
    root: str = Field(min_length=1, description="workspace root 名称")
    changed: bool | None = Field(
        default=None,
        description="要求最终 get_changes 显示有改动、无改动或不限定",
    )
    allowed_paths: list[str] = Field(
        default_factory=list,
        description="允许出现在最终 Git status 中的相对路径",
    )


class TaskVerificationInput(BaseModel):
    commands: list[CommandRequirementInput] = Field(
        default_factory=list,
        description="本 Task completed 前必须真实成功执行的命令",
    )
    workspace: WorkspaceRequirementInput | None = Field(
        default=None,
        description="本 Task completed 前必须满足的 get_changes 结果",
    )


class TaskDraftInput(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    depends_on: list[str]
    acceptance_criteria: str | None = None
    verification: TaskVerificationInput | None = None


class DependencyChangeInput(BaseModel):
    task_id: str = Field(min_length=1)
    depends_on: list[str]


class SubmitPlanRevisionInput(BaseModel):
    reason: str = Field(min_length=1)
    replacement_tasks: list[TaskDraftInput] = Field(min_length=1)
    dependency_changes: list[DependencyChangeInput]


def create_submit_task_outcome_spec(
    command_buffer: GoalCommandBuffer,
) -> ToolSpec:
    def submit(data: SubmitTaskOutcomeInput) -> dict:
        try:
            command_buffer.submit_task_outcome(
                TaskOutcome(
                    task_id=command_buffer.task_id,
                    run_id=command_buffer.run_id,
                    decision=data.decision,
                    summary=data.summary,
                    evidence=tuple(data.evidence),
                )
            )
        except ValueError as exc:
            return {
                "ok": False,
                "code": "INVALID_TASK_OUTCOME_COMMAND",
                "error": str(exc),
                "hint": "修正 TaskOutcome 后重试。",
            }
        return {
            "ok": True,
            "code": "TASK_OUTCOME_BUFFERED",
            "data": {
                "task_id": command_buffer.task_id,
                "run_id": command_buffer.run_id,
                "decision": data.decision.value,
            },
            "error": None,
            "hint": (
                "若未收到系统明确的验收拒绝，不要再次调用本工具；"
                "直接给出当前 Run 的最终回答。"
            ),
        }

    return ToolSpec(
        name=SUBMIT_TASK_OUTCOME,
        description=(
            "提交当前 Goal Task 的明确执行结论。仅在当前 Task 已完成本次执行和验收后调用；"
            "continue 表示后续 Run 继续当前 Task，completed 表示验收通过，replan 表示请求重规划。"
            "先完成全部工作、验收和 TodoList 最终同步，再把本工具作为最后一个工具调用。"
            "成功后直接给出最终回答；只有系统明确拒绝验收时才补充工作并修订结论。"
        ),
        input_model=SubmitTaskOutcomeInput,
        handler=submit,
    )


def create_submit_plan_revision_spec(
    command_buffer: GoalCommandBuffer,
    validate_revision: Callable[[PlanRevision], None],
) -> ToolSpec:
    def submit(data: SubmitPlanRevisionInput) -> dict:
        try:
            revision = PlanRevision(
                task_id=command_buffer.task_id,
                reason=data.reason,
                replacement_tasks=tuple(
                    TaskDraft(
                        id=task.id,
                        description=task.description,
                        depends_on=tuple(task.depends_on),
                        acceptance_criteria=task.acceptance_criteria,
                        verification=(
                            TaskVerification(
                                commands=tuple(
                                    CommandRequirement(
                                        command_contains=requirement.command_contains,
                                        root=requirement.root,
                                        cwd=requirement.cwd,
                                        expected_exit_codes=(
                                            tuple(requirement.expected_exit_codes)
                                            if requirement.expected_exit_codes is not None
                                            else None
                                        ),
                                    )
                                    for requirement in task.verification.commands
                                ),
                                workspace=(
                                    WorkspaceRequirement(
                                        root=task.verification.workspace.root,
                                        changed=task.verification.workspace.changed,
                                        allowed_paths=tuple(
                                            task.verification.workspace.allowed_paths
                                        ),
                                    )
                                    if task.verification.workspace is not None
                                    else None
                                ),
                            )
                            if task.verification is not None
                            else None
                        ),
                    )
                    for task in data.replacement_tasks
                ),
                dependency_changes=tuple(
                    DependencyChange(
                        task_id=change.task_id,
                        depends_on=tuple(change.depends_on),
                    )
                    for change in data.dependency_changes
                ),
            )
            validate_revision(revision)
            command_buffer.submit_plan_revision(revision)
        except ValueError as exc:
            return {
                "ok": False,
                "code": "INVALID_PLAN_REVISION_COMMAND",
                "error": str(exc),
                "hint": "修正 Task ID、依赖关系或重复提交后重试。",
            }
        return {
            "ok": True,
            "code": "PLAN_REVISION_BUFFERED",
            "data": {
                "task_id": command_buffer.task_id,
                "run_id": command_buffer.run_id,
            },
            "error": None,
            "hint": None,
        }

    return ToolSpec(
        name=SUBMIT_PLAN_REVISION,
        description=(
            "提交当前 Goal 的显式任务图修订。仅在 Goal 要求重规划时调用；提供替代 Task 和必要的"
            "依赖修改，不得删除或覆盖历史 Task。工具把修订暂存到当前 Run，Run 正常完成后由 Goal "
            "整体校验并应用；非法 ID、依赖或环会被拒绝。"
        ),
        input_model=SubmitPlanRevisionInput,
        handler=submit,
    )


@dataclass(frozen=True)
class GoalTaskCapability:
    goal_id: str
    objective: str
    task: Task
    command_buffer: GoalCommandBuffer
    completion_gate: CompletionGate = field(default_factory=CompletionGate)

    def include_base_tools(self) -> bool:
        return True

    def evidence_roots(self) -> tuple[str, ...]:
        verification = self.task.verification
        if verification is None or verification.workspace is None:
            return ()
        return (verification.workspace.root,)

    def runtime_instructions(self) -> list[str]:
        acceptance = self.task.acceptance_criteria or "未单独声明"
        return [
            "\n".join(
                [
                    "你正在执行一个跨 Run Goal 中的当前 Task。",
                    f"Goal ID：{self.goal_id}",
                    f"Goal 目标：{self.objective}",
                    f"Task ID：{self.task.id}",
                    f"Task：{self.task.description}",
                    f"验收标准：{acceptance}",
                    self._verification_instruction(),
                    "顺序要求：先完成工作与验收，再完成 TodoList 最终同步，"
                    "最后调用 submit_task_outcome，随后直接给出最终回答。",
                    "通常只调用一次；只有收到系统明确的验收拒绝时，才补充工作并重新提交。",
                    "TodoList 只描述本 Run 的执行过程，不能代替 TaskOutcome。",
                ]
            )
        ]

    def tool_specs(self) -> list[ToolSpec]:
        return [create_submit_task_outcome_spec(self.command_buffer)]

    def check_final_candidate(self, evidence: RunEvidence) -> str | None:
        if self.command_buffer.task_outcome is None:
            return "结束当前 Goal Task Run 前必须调用 submit_task_outcome。"
        outcome = self.command_buffer.task_outcome
        if outcome.decision is OutcomeDecision.COMPLETED:
            review = self.completion_gate.review(
                self.task.verification,
                self.task.acceptance_criteria,
                evidence,
            )
            if not review.accepted:
                return (
                    "当前 completed 结论未通过系统验收："
                    f"{review.reason} 请继续执行缺失验证；若验收条件不可满足，"
                    "重新调用 submit_task_outcome 提交 continue 或 replan。"
                )
        return None

    def _verification_instruction(self) -> str:
        verification = self.task.verification
        if verification is None:
            return "当前 Task 没有结构化 verification contract。"
        lines = ["系统将在结束前核验以下事实："]
        for requirement in verification.commands:
            expectation = (
                "命令真实完成"
                if requirement.expected_exit_codes is None
                else f"退出码属于 {list(requirement.expected_exit_codes)}"
            )
            lines.append(
                f"- 命令：{requirement.command_contains}；{expectation}"
            )
        if verification.workspace is not None:
            workspace = verification.workspace
            state = (
                "存在改动"
                if workspace.changed is True
                else "保持无改动"
                if workspace.changed is False
                else "已执行 get_changes"
            )
            line = f"- Workspace {workspace.root}：{state}"
            if workspace.allowed_paths:
                line += f"；允许路径：{list(workspace.allowed_paths)}"
            lines.append(line)
        return "\n".join(lines)

    def checkpoint_data(self) -> dict | None:
        outcome = self.command_buffer.task_outcome
        if outcome is None:
            return None
        return {
            "goal_command": {
                "kind": "task_outcome",
                "goal_id": self.goal_id,
                "task_id": outcome.task_id,
                "run_id": outcome.run_id,
                "decision": outcome.decision.value,
            }
        }


@dataclass(frozen=True)
class GoalPlanRevisionCapability:
    goal: Goal
    task_id: str
    command_buffer: GoalCommandBuffer

    def include_base_tools(self) -> bool:
        return False

    def evidence_roots(self) -> tuple[str, ...]:
        return ()

    def runtime_instructions(self) -> list[str]:
        task_lines = [
            f"- {task.id}: {task.description}; status={task.status.value}; "
            f"depends_on={list(task.depends_on)}"
            for task in self.goal.tasks
        ]
        latest = self.goal.outcomes[-1]
        return [
            "\n".join(
                [
                    "当前 Goal 明确要求重规划。",
                    "本 Run 只负责修订任务图，不执行 replacement Task，不读写 workspace。",
                    f"Goal ID：{self.goal.id}",
                    f"Goal 目标：{self.goal.objective}",
                    f"需要替换的 Task：{self.task_id}",
                    f"重规划原因：{latest.summary}",
                    "当前任务图：",
                    *task_lines,
                    "replacement Task 若声明 acceptance_criteria，必须同时提供 verification。",
                    "verification.commands 使用稳定 command_contains，并可约束 root/cwd；"
                    "expected_exit_codes 默认 [0]，诊断命令只要求真实执行时使用 null；",
                    "verification.workspace 可约束 changed 和 allowed_paths。",
                    "结束本 Run 前必须调用且只调用一次 submit_plan_revision。",
                ]
            )
        ]

    def tool_specs(self) -> list[ToolSpec]:
        return [
            create_submit_plan_revision_spec(
                self.command_buffer,
                self.goal.validate_plan_revision,
            )
        ]

    def check_final_candidate(self, evidence: RunEvidence) -> str | None:
        if self.command_buffer.plan_revision is None:
            return "结束重规划 Run 前必须调用 submit_plan_revision。"
        return None

    def checkpoint_data(self) -> dict | None:
        revision = self.command_buffer.plan_revision
        if revision is None:
            return None
        return {
            "goal_command": {
                "kind": "plan_revision",
                "goal_id": self.goal.id,
                "task_id": revision.task_id,
                "run_id": self.command_buffer.run_id,
            }
        }
