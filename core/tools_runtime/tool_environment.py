from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.tool_registry import ToolSpec
from core.runtime_modes import RuntimeMode
from core.tools_runtime.progressive_toolsets import (
    ToolsetLoadingState,
    create_load_toolset_spec,
    toolset_catalog_instruction,
)
from core.tools_runtime.progressive_skills import (
    SkillBudget,
    SkillLoadingState,
    create_load_skill_spec,
    create_read_skill_resource_spec,
    skill_runtime_instructions,
)
from core.tools_runtime.turn_invocation import TurnInvocation
from core.tools_runtime.tools_executor import ToolsExecutor


@dataclass(frozen=True)
class ToolEnvironmentSnapshot:
    executor: ToolsExecutor
    model_tools: list[dict]
    runtime_prompts: list[str]


class TurnToolEnvironment:
    """管理一次 Turn 内的工具选择、渐进加载和逐 AgentStep 可见快照。"""

    def __init__(
        self,
        tools_executor: ToolsExecutor,
        invocation: TurnInvocation,
        environment_tool_specs: tuple[ToolSpec, ...] = (),
    ) -> None:
        self.invocation = invocation
        if environment_tool_specs:
            environment_registry = tools_executor.registry.clone()
            for spec in environment_tool_specs:
                environment_registry.register(spec)
            tools_executor = ToolsExecutor(environment_registry)
        if invocation.capabilities:
            selections = [
                capability.base_tool_names()
                for capability in invocation.capabilities
            ]
            restrictions = [
                set(selection)
                for selection in selections
                if selection is not None
            ]
            self.base_registry = (
                tools_executor.registry.select(set.intersection(*restrictions))
                if restrictions
                else tools_executor.registry.clone()
            )
            for capability in invocation.capabilities:
                for spec in capability.tool_specs():
                    self.base_registry.register(spec)
            self.base_executor = ToolsExecutor(self.base_registry)
        else:
            self.base_registry = tools_executor.registry
            self.base_executor = tools_executor

        self.toolset_provider = invocation.toolset_provider
        self.toolset_descriptors = (
            self.toolset_provider.descriptors()
            if self.toolset_provider is not None
            else ()
        )
        self.toolset_state = ToolsetLoadingState()
        self.skill_provider = invocation.skill_provider
        self.skill_descriptors = (
            self.skill_provider.descriptors()
            if self.skill_provider is not None
            else ()
        )
        self.skill_state = SkillLoadingState()
        self.skill_budget = SkillBudget()

    def evidence_roots(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            root
            for capability in self.invocation.capabilities
            for root in capability.evidence_roots()
        ))

    def snapshot(self, runtime_mode: RuntimeMode, mode_state: Any) -> ToolEnvironmentSnapshot:
        if self.toolset_provider is None and self.skill_provider is None:
            turn_registry = self.base_registry
            turn_executor = self.base_executor
        else:
            turn_registry = self.base_registry.clone()
            if self.toolset_provider is not None:
                turn_registry.register(
                    create_load_toolset_spec(
                        self.toolset_descriptors,
                        self.toolset_state,
                        self.toolset_provider,
                    )
                )
                for descriptor in self.toolset_descriptors:
                    loaded_specs = self.toolset_state.loaded_specs.get(descriptor.id)
                    if loaded_specs is None:
                        continue
                    for spec in loaded_specs:
                        turn_registry.register(spec)
            if self.skill_provider is not None:
                turn_registry.register(create_load_skill_spec(
                    self.skill_descriptors,
                    self.skill_state,
                    self.skill_provider,
                    self.skill_budget,
                ))
                turn_registry.register(create_read_skill_resource_spec(
                    self.skill_state,
                    self.skill_provider,
                ))
            turn_executor = ToolsExecutor(turn_registry)

        external_tools = turn_registry.get_tools()
        runtime_tools = runtime_mode.runtime_tools(mode_state)
        external_names = {
            tool["function"]["name"] for tool in external_tools
        }
        runtime_names = {
            tool["function"]["name"] for tool in runtime_tools
        }
        duplicated_names = external_names & runtime_names
        if duplicated_names:
            raise ValueError(
                "runtime tool conflicts with external tool: "
                f"{sorted(duplicated_names)}"
            )

        runtime_prompts = list(runtime_mode.runtime_instructions(mode_state))
        for capability in self.invocation.capabilities:
            runtime_prompts.extend(capability.runtime_instructions())
        if self.toolset_provider is not None:
            runtime_prompts.append(
                toolset_catalog_instruction(
                    self.toolset_descriptors,
                    self.toolset_state,
                )
            )
        if self.skill_provider is not None:
            runtime_prompts.extend(skill_runtime_instructions(
                self.skill_descriptors,
                self.skill_state,
                self.skill_budget,
            ))

        return ToolEnvironmentSnapshot(
            executor=turn_executor,
            model_tools=external_tools + runtime_tools,
            runtime_prompts=runtime_prompts,
        )
