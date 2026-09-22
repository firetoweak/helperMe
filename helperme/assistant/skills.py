from __future__ import annotations

from collections.abc import Mapping

from helperme.assistant.artifacts import ArtifactGateway
from helperme.assistant.context.projection import (
    ModelContextSettings,
    externalize_tool_result,
)
from helperme.runtime import ToolBinding
from helperme.runtime.dispatcher import AttemptContext
from helperme.assistant.tool_results import runtime_tool_result
from helperme.tools.spec import ToolArgumentsError
from helperme.skills.runtime import LOAD_SKILL, READ_SKILL_RESOURCE
from helperme.assistant.catalog import CatalogSkill


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
        self._catalogs: dict[str, dict[str, int]] = {}

    def registry_catalog(self) -> list[dict[str, object]]:
        self._catalog.tool_specs()
        return sorted(
            (
                {
                    "id": record.name,
                    "description": record.description,
                    "revision": record.revision,
                }
                for record in self._catalog.registry.snapshot()
                if record.enabled
            ),
            key=lambda item: item["id"],
        )

    def apply_catalog(
        self,
        session_id: str,
        skills: tuple[CatalogSkill, ...],
    ) -> None:
        self._catalogs[session_id] = {
            item.id: item.revision for item in skills
        }

    def schemas(self) -> list[dict[str, object]]:
        return [spec.to_openai_tool() for spec in self._catalog.tool_specs({})]

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
            catalog_specs = self._catalog.tool_specs(
                self._catalogs[context.session_id]
            )
            specs = {spec.name: spec for spec in catalog_specs}
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
                    "code": "VALIDATION_ERROR",
                    "data": {"details": exc.details},
                    "error": "skill arguments validation failed",
                }
            result = runtime_tool_result(await spec.handler(payload))
            return externalize_tool_result(
                result,
                context.session_id,
                self._gateway,
                self._settings,
            )

        return handler
