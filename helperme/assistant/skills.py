from __future__ import annotations

from collections.abc import Mapping

from helperme.assistant.artifacts import ArtifactGateway
from helperme.assistant.context.projection import (
    ModelContextSettings,
    externalize_payload,
)
from helperme.runtime import ToolBinding
from helperme.runtime.dispatcher import AttemptContext
from helperme.assistant.tool_results import runtime_tool_result
from helperme.tools.spec import ToolArgumentsError
from helperme.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE


class SkillToolAdapter:
    """Project enabled Skills as two ordinary Runtime tools."""

    def __init__(
        self,
        skills,
        gateway: ArtifactGateway,
        settings: ModelContextSettings,
    ) -> None:
        self.skills = skills
        self._catalog = skills.tool_catalog
        self._gateway = gateway
        self._settings = settings

    def schemas(self) -> list[dict[str, object]]:
        return [spec.to_openai_tool() for spec in self._catalog.tool_specs()]

    def bindings(self) -> dict[str, ToolBinding]:
        return {
            LOAD_SKILL: ToolBinding(self._handler(LOAD_SKILL)),
            READ_SKILL_RESOURCE: ToolBinding(self._handler(READ_SKILL_RESOURCE)),
        }

    def _handler(self, name: str):
        async def handler(
            context: AttemptContext,
            arguments: Mapping[str, object],
        ) -> object:
            catalog_specs = self._catalog.tool_specs()
            specs = {spec.name: spec for spec in catalog_specs}
            if len(specs) != len(catalog_specs):
                raise ValueError("Skill tool catalog 包含重复 name")
            spec = specs.get(name)
            if spec is None:
                return {
                    "ok": False,
                    "code": "SKILL_NOT_AVAILABLE",
                    "error": "当前没有已启用的 Skill",
                    "hint": "使用 /skill list 查看，或 /skill enable 启用。",
                }
            try:
                payload = spec.parameters.validate(dict(arguments))
            except ToolArgumentsError as exc:
                return {
                    "ok": False,
                    "code": "INVALID_ARGUMENT",
                    "data": {"details": exc.details},
                    "error": "skill arguments validation failed",
                }
            result = runtime_tool_result(await spec.handler(payload))
            body, _artifact_id = externalize_payload(
                result,
                self._gateway.for_stream(context.stream_id),
                max_chars=self._settings.size_externalize_chars,
                preview_chars=self._settings.preview_chars,
            )
            return body

        return handler
