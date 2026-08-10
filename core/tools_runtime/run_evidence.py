from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolEvidence:
    call_id: str
    name: str
    arguments: str
    result: dict[str, Any]


@dataclass(frozen=True)
class WorkspaceBaseline:
    root: str
    result: dict[str, Any]


@dataclass(frozen=True)
class RunEvidence:
    steps: tuple[ToolEvidence, ...] = ()
    workspace_baselines: tuple[WorkspaceBaseline, ...] = ()

    def by_name(self, name: str) -> tuple[ToolEvidence, ...]:
        return tuple(step for step in self.steps if step.name == name)

    def workspace_baseline(self, root: str) -> WorkspaceBaseline | None:
        return next(
            (
                baseline
                for baseline in self.workspace_baselines
                if baseline.root == root
            ),
            None,
        )


class RunEvidenceRecorder:
    def __init__(self) -> None:
        self._steps: list[ToolEvidence] = []
        self._workspace_baselines: list[WorkspaceBaseline] = []

    def record_workspace_baseline(
        self,
        root: str,
        result: dict[str, Any],
    ) -> None:
        self._workspace_baselines.append(
            WorkspaceBaseline(root, deepcopy(result))
        )

    def record(
        self,
        call_id: str,
        name: str,
        arguments: str,
        result: dict[str, Any],
    ) -> None:
        self._steps.append(
            ToolEvidence(
                call_id=call_id,
                name=name,
                arguments=arguments,
                result=deepcopy(result),
            )
        )

    def snapshot(self) -> RunEvidence:
        return RunEvidence(
            tuple(self._steps),
            tuple(self._workspace_baselines),
        )
