from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import AbstractSet, Protocol

from helperme.assistant.artifacts import ArtifactGateway
from helperme.assistant.control import (
    AssistantControlPlane,
    ControlArgumentsError,
)
from helperme.assistant.delivery import DELIVER_TOOL_NAME, ensure_deliver
from helperme.assistant.context.projection import (
    ModelContextProjector,
    ModelContextSettings,
    externalize_payload,
)
from helperme.assistant.context.prompt import DEFAULT_ASSISTANT_PROMPT
from helperme.assistant.toolsets import ToolSurface
from helperme.assistant.management import ManagementSurface
from helperme.runtime import (
    InvokeTool,
    ModelDecision,
    RecordedDecision,
    ToolBinding,
)
from helperme.runtime.dispatcher import AttemptContext
from helperme.runtime.state import DecisionFrame
from helperme.assistant.skills import SkillToolAdapter
from helperme.llm.api import (
    InvalidLLMResponse,
    LLMApi,
    LLMResponse,
    ToolCall,
)


class ToolRunner(Protocol):
    def names(self) -> Sequence[str]: ...

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, object],
    ) -> object: ...

    def requires_authorization(self, name: str) -> bool: ...


def decision_from_llm(
    response: LLMResponse,
    allowed_tool_names: AbstractSet[str],
) -> ModelDecision:
    return ModelDecision(
        content=response.content,
        command_requests=_invoke_requests(
            response.calls,
            allowed_tool_names,
        ),
    )


def _invoke_requests(
    calls: Sequence[ToolCall],
    allowed_tool_names: AbstractSet[str],
) -> tuple[InvokeTool, ...]:
    requests: list[InvokeTool] = []
    for call in calls:
        if call.name == DELIVER_TOOL_NAME:
            raise InvalidLLMResponse(
                "invalid_tool_call",
                "deliver is a product command, not a model tool",
            )
        if call.name not in allowed_tool_names:
            raise InvalidLLMResponse(
                "unknown_tool",
                f"tool {call.name} was not offered in this decision context",
            )
        try:
            payload = json.loads(call.arguments)
        except json.JSONDecodeError as exc:
            raise InvalidLLMResponse(
                "invalid_tool_arguments",
                f"tool {call.name} arguments are not valid JSON",
            ) from exc
        if type(payload) is not dict:
            raise InvalidLLMResponse(
                "invalid_tool_arguments",
                f"tool {call.name} arguments must be a JSON object",
            )
        requests.append(InvokeTool(call.name, tuple(payload.items())))
    return tuple(requests)


def _tool_names(
    schemas: Sequence[dict[str, object]],
) -> frozenset[str]:
    names: list[str] = []
    for schema in schemas:
        if set(schema) != {"type", "function"} or schema["type"] != "function":
            raise ValueError("tool schema envelope is invalid")
        function = schema["function"]
        if not isinstance(function, Mapping):
            raise TypeError("tool schema function must be an object")
        name = function["name"]
        if type(name) is not str or not name:
            raise ValueError("tool schema name must be a non-empty str")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("tool schemas contain duplicate names")
    return frozenset(names)


def bind_executor_tools(
    runner: ToolRunner,
    gateway: ArtifactGateway,
    settings: ModelContextSettings,
) -> dict[str, ToolBinding]:
    bindings: dict[str, ToolBinding] = {}
    for name in runner.names():
        bindings[name] = ToolBinding(
            _executor_handler(runner, name, gateway, settings),
            requires_authorization=runner.requires_authorization(name),
        )
    return bindings


def _executor_handler(
    runner: ToolRunner,
    name: str,
    gateway: ArtifactGateway,
    settings: ModelContextSettings,
):
    async def handler(
        context: AttemptContext,
        arguments: Mapping[str, object],
    ) -> object:
        result = await runner.execute(name, arguments)
        payload, _artifact_id = externalize_payload(
            result,
            gateway.for_session(context.session_id),
            max_chars=settings.size_externalize_chars,
            preview_chars=settings.preview_chars,
        )
        return payload

    return handler


class JournalBackedLlmDecisionMaker:
    def __init__(
        self,
        journal,
        llm: LLMApi,
        model: str,
        system_prompt: str = DEFAULT_ASSISTANT_PROMPT,
        projector: ModelContextProjector | None = None,
        surface: ToolSurface | None = None,
        skill_tools: SkillToolAdapter | None = None,
        control: AssistantControlPlane | None = None,
        management: ManagementSurface | None = None,
        context_usage_sink: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self._journal = journal
        self._llm = llm
        self._model = model
        self._system_prompt = system_prompt
        self._projector = ModelContextProjector() if projector is None else projector
        self._surface = surface
        self._skill_tools = skill_tools
        self._control = control
        self._management = management
        self._context_usage_sink = context_usage_sink

    def _schemas(
        self,
        frame: DecisionFrame,
    ) -> tuple[list[dict[str, object]], frozenset[str]]:
        if self._surface is not None:
            schemas = self._surface.schemas(
                frame.state.session_id,
                frame.state,
            )
        else:
            schemas = []
        if self._skill_tools is not None:
            schemas = [*schemas, *self._skill_tools.schemas()]
        if self._management is not None:
            schemas = [
                *schemas,
                *self._management.schemas(frame.state.session_id, frame.state),
            ]
        offered_control_names = frozenset()
        if self._control is not None:
            allowed_control_names = (
                None
                if self._management is None
                else self._management.control_names(
                    frame.state.session_id,
                    frame.state,
                )
            )
            control_schemas = self._control.schemas(
                frame.state.session_id,
                allowed_control_names,
            )
            offered_control_names = _tool_names(control_schemas)
            schemas = [
                *schemas,
                *control_schemas,
            ]
        return deepcopy(schemas), offered_control_names

    def _decision_from_response(
        self,
        frame: DecisionFrame,
        response: LLMResponse,
        allowed_tool_names: AbstractSet[str],
        control_names: AbstractSet[str],
    ) -> ModelDecision:
        control_calls = tuple(
            call for call in response.calls if call.name in control_names
        )
        if control_calls and len(response.calls) != 1:
            raise InvalidLLMResponse(
                "invalid_control_batch",
                "a host control tool must be the only tool call",
            )
        if control_calls:
            call = control_calls[0]
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError as exc:
                raise InvalidLLMResponse(
                    "invalid_tool_arguments",
                    f"tool {call.name} arguments are not valid JSON",
                ) from exc
            if type(arguments) is not dict:
                raise InvalidLLMResponse(
                    "invalid_tool_arguments",
                    f"tool {call.name} arguments must be a JSON object",
                )
            if self._control is None:
                raise RuntimeError("control call accepted without control plane")
            try:
                self._control.stage(frame, call.name, arguments)
            except ControlArgumentsError as exc:
                raise InvalidLLMResponse(
                    "invalid_tool_arguments",
                    f"tool {call.name} arguments violate its schema: {exc.details}",
                ) from exc
            return ModelDecision(
                content=(
                    response.content or "已提交控制操作方案，等待主机生成确认信息。"
                ),
            )

        return ModelDecision(
            content=response.content,
            command_requests=_invoke_requests(
                response.calls,
                allowed_tool_names,
            ),
        )

    async def decide(self, frame: DecisionFrame) -> RecordedDecision:
        # Host-owned context is captured before the first await. Journal facts
        # are bounded by the frame position, freezing this Step's visible world.
        prompt = self._system_prompt
        catalog = (
            self._surface.catalog_instruction(
                frame.state.session_id,
                frame.state,
            )
            if self._surface is not None
            else None
        )
        schemas, control_names = self._schemas(frame)
        allowed_tool_names = _tool_names(schemas)
        journal_tail = await self._journal.snapshot(frame.state.session_id)
        events = tuple(
            event
            for event in journal_tail
            if event.sequence <= frame.observed_journal_position
        )
        if catalog is not None:
            prompt = f"{prompt}\n\n{catalog}"
        if self._management is not None:
            management_catalog = self._management.catalog_instruction(
                frame.state.session_id,
                frame.state,
            )
            prompt = f"{prompt}\n\n{management_catalog}"
        prepared = self._projector.prepare(
            events,
            frame.state.visible_event_ids,
            frame.state.session_id,
            prompt,
            schemas,
        )
        if self._context_usage_sink is not None:
            estimated = self._projector.budget.assess(
                prepared.messages,
                schemas,
            ).estimated_input_tokens
            self._context_usage_sink(
                frame.state.session_id,
                estimated,
                self._projector.settings.context_limit,
            )
        result = await self._llm.chat(
            prepared.messages,
            self._model,
            tools=schemas or None,
        )
        usage = result.usage
        if self._context_usage_sink is not None:
            self._context_usage_sink(
                frame.state.session_id,
                usage.input_tokens,
                self._projector.settings.context_limit,
            )
        if usage.input_tokens > 0:
            self._projector.budget.observe_actual_usage(
                prepared.messages,
                schemas,
                usage.input_tokens,
            )
        decision = ensure_deliver(
            self._decision_from_response(
                frame,
                result.response,
                allowed_tool_names,
                control_names,
            )
        )
        manifest = {
            "schema": "decision-replay-manifest/v1",
            "decision_basis": {
                "trigger_event_id": frame.trigger_event.event_id,
                "decision_cursor": frame.decision_cursor,
                "basis_state_version": frame.basis_state_version,
                "observed_journal_position": frame.observed_journal_position,
                "visible_event_ids": list(frame.state.visible_event_ids),
            },
            "request": {
                "projector": "model-context/v1",
                "model": self._model,
                "messages": prepared.messages,
                "tools": schemas or None,
            },
            "response": {
                "content": result.response.content,
                "calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                    for call in result.response.calls
                ],
            },
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
            },
        }
        artifact = self._projector.gateway.for_session(frame.state.session_id).save(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        )
        return RecordedDecision(decision, (artifact.artifact_id,))
