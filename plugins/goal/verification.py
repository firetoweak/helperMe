from __future__ import annotations

import json
from dataclasses import dataclass

from core.tools_runtime.turn_evidence import TurnEvidence, ToolEvidence


@dataclass(frozen=True)
class CommandRequirement:
    command_contains: str
    workspace_root_id: str | None = None
    cwd: str | None = None
    expected_exit_codes: tuple[int, ...] | None = (0,)

    def __post_init__(self) -> None:
        if not self.command_contains.strip():
            raise ValueError("command_contains cannot be empty")
        if self.expected_exit_codes is not None and not self.expected_exit_codes:
            raise ValueError("expected_exit_codes cannot be empty")


@dataclass(frozen=True)
class WorkspaceRequirement:
    root_id: str
    changed: bool | None = None
    allowed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.root_id.strip():
            raise ValueError("workspace requirement root cannot be empty")
        if any(not path.strip() for path in self.allowed_paths):
            raise ValueError("allowed workspace paths cannot be empty")


@dataclass(frozen=True)
class GoalVerification:
    commands: tuple[CommandRequirement, ...] = ()
    workspace: WorkspaceRequirement | None = None


@dataclass(frozen=True)
class CompletionReview:
    accepted: bool
    reason: str


class CompletionGate:
    """只验证 Judge Turn 中真实发生的机械事实，不解释 Goal 语义。"""

    def review(
        self,
        verification: GoalVerification,
        evidence: TurnEvidence,
    ) -> CompletionReview:
        failures = [
            failure
            for requirement in verification.commands
            if (
                failure := self._command_failure(requirement, evidence)
            ) is not None
        ]
        if verification.workspace is not None:
            workspace_failure = self._workspace_failure(
                verification.workspace,
                evidence,
            )
            if workspace_failure is not None:
                failures.append(workspace_failure)

        if failures:
            return CompletionReview(False, "；".join(failures))
        return CompletionReview(True, "结构化验收事实全部满足。")

    @staticmethod
    def _command_failure(
        requirement: CommandRequirement,
        evidence: TurnEvidence,
    ) -> str | None:
        matching: list[ToolEvidence] = []
        for step in evidence.by_name("execute_command"):
            arguments = json.loads(step.arguments)
            if requirement.command_contains not in arguments["command"]:
                continue
            data = step.result.get("data") or {}
            membership = data.get("workspace_membership") or {}
            if (
                requirement.workspace_root_id is not None
                and membership.get("root_id") != requirement.workspace_root_id
            ):
                continue
            if requirement.cwd is not None and data.get("cwd") != requirement.cwd:
                continue
            matching.append(step)

        label = requirement.command_contains
        if not matching:
            return f"缺少命令验收证据：{label}"
        if not any(
            CompletionGate._command_satisfied(
                step,
                requirement.expected_exit_codes,
            )
            for step in matching
        ):
            if requirement.expected_exit_codes is None:
                return f"命令没有正常完成：{label}"
            return (
                f"命令退出码不符合验收要求 {requirement.expected_exit_codes}："
                f"{label}"
            )
        return None

    @staticmethod
    def _command_satisfied(
        step: ToolEvidence,
        expected_exit_codes: tuple[int, ...] | None,
    ) -> bool:
        result = step.result
        data = result.get("data") or {}
        completed = (
            result.get("ok") is True
            and result.get("code") == "COMMAND_COMPLETED"
            and data.get("timed_out") is False
            and isinstance(data.get("exit_code"), int)
        )
        if not completed or expected_exit_codes is None:
            return completed
        return data["exit_code"] in expected_exit_codes

    @staticmethod
    def _workspace_failure(
        requirement: WorkspaceRequirement,
        evidence: TurnEvidence,
    ) -> str | None:
        matching = [
            step
            for step in evidence.by_name("get_changes")
            if step.result.get("ok") is True
            and (
                (step.result.get("data") or {})
                .get("workspace_membership", {})
                .get("root_id")
                == requirement.root_id
            )
        ]
        if not matching:
            return f"缺少 Workspace 验收证据：{requirement.root_id}"

        data = matching[-1].result["data"]
        changed = bool(data.get("changed"))
        if requirement.changed is not None and changed is not requirement.changed:
            expected = "存在改动" if requirement.changed else "保持无改动"
            return f"Workspace {requirement.root_id} 未{expected}"

        if requirement.allowed_paths:
            actual_paths = CompletionGate._status_paths(data.get("status") or "")
            unexpected = sorted(actual_paths - set(requirement.allowed_paths))
            if unexpected:
                return "Workspace 出现契约外改动：" + ", ".join(unexpected)
        return None

    @staticmethod
    def _status_paths(status: str) -> set[str]:
        paths: set[str] = set()
        for line in status.splitlines():
            value = line[3:].strip()
            if " -> " in value:
                paths.update(part.strip() for part in value.split(" -> ", 1))
            elif value:
                paths.add(value)
        return paths
