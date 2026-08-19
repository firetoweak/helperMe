from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from core.environment import EnvironmentLocation


@dataclass(frozen=True)
class EvidenceOrigin:
    tool_call_id: str
    tool_name: str


@dataclass(frozen=True)
class ToolEvidence:
    call_id: str
    name: str
    arguments: str
    result: dict[str, Any]

    @property
    def origin(self) -> EvidenceOrigin:
        return EvidenceOrigin(self.call_id, self.name)


@dataclass(frozen=True)
class EnvironmentBaseline:
    root_id: str
    location: EnvironmentLocation
    result: dict[str, Any]


@dataclass(frozen=True)
class TurnEvidence:
    steps: tuple[ToolEvidence, ...] = ()
    environment_baselines: tuple[EnvironmentBaseline, ...] = ()

    def by_name(self, name: str) -> tuple[ToolEvidence, ...]:
        return tuple(step for step in self.steps if step.name == name)

    def environment_baseline(
        self,
        root_id: str,
    ) -> EnvironmentBaseline | None:
        return next(
            (
                baseline
                for baseline in self.environment_baselines
                if baseline.root_id == root_id
            ),
            None,
        )


class TurnEvidenceRecorder:
    def __init__(self) -> None:
        self._steps: list[ToolEvidence] = []
        self._environment_baselines: list[EnvironmentBaseline] = []

    def record_environment_baseline(
        self,
        root_id: str,
        result: dict[str, Any],
    ) -> None:
        location = EnvironmentLocation.from_dict(result["data"]["location"])
        self._environment_baselines.append(
            EnvironmentBaseline(root_id, location, deepcopy(result))
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

    def snapshot(self) -> TurnEvidence:
        return TurnEvidence(
            tuple(self._steps),
            tuple(self._environment_baselines),
        )
