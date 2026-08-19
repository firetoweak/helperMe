from __future__ import annotations

import json
from pathlib import Path

from plugins.skills.application import SkillApplicationService
from plugins.skills.models import SkillSourceRef


class SkillCommandError(ValueError):
    pass


class SkillConsoleAdapter:
    def __init__(self, service: SkillApplicationService) -> None:
        self._service = service

    async def execute_if_handled(self, user_message: str) -> str | None:
        if not user_message.startswith("/skill"):
            return None
        parts = user_message.split(maxsplit=2)
        if len(parts) == 1 or parts[1] == "help":
            return self._help()
        action = parts[1]
        rest = parts[2] if len(parts) > 2 else ""
        try:
            if action == "list":
                return await self._list()
            if action == "install":
                return await self._install(rest)
            if action == "inspect":
                return await self._inspect(rest)
            if action == "test":
                return await self._test(rest)
            if action == "enable":
                return self._with_new_session(await self._set_enabled(rest, True))
            if action == "disable":
                return self._with_new_session(await self._set_enabled(rest, False))
            if action == "remove":
                return self._with_new_session(await self._remove(rest))
            if action == "check-update":
                return await self._check_update(rest)
            if action == "update":
                return self._with_new_session(await self._update(rest))
        except KeyError as exc:
            raise SkillCommandError(f"未找到 Skill: {exc}") from exc
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise SkillCommandError(str(exc)) from exc
        raise SkillCommandError(f"未知 /skill 子命令: {action}")

    async def _list(self) -> str:
        records = await self._service.list_skills()
        if not records:
            return "尚未安装任何 Skill。"
        return "\n".join(
            f"- {item.name} [{'enabled' if item.enabled else 'disabled'}] "
            f"{item.description} (revision={item.revision})"
            for item in records
        )

    async def _install(self, rest: str) -> str:
        source = rest.strip()
        if not source:
            raise SkillCommandError(
                "/skill install [local|url|github] <locator> [ref]"
            )
        parts = source.split(maxsplit=2)
        if parts[0] in {"local", "url", "github"}:
            if len(parts) < 2:
                raise SkillCommandError(
                    "/skill install [local|url|github] <locator> [ref]"
                )
            kind = parts[0]
            locator = parts[1]
            requested_ref = parts[2] if len(parts) == 3 else None
            source_ref = SkillSourceRef(kind, locator, requested_ref)
        else:
            source_ref = SkillSourceRef("local", str(Path(source).resolve()))
        record = await self._service.install_source(source_ref)
        return (
            f"已安装 Skill `{record.name}` 为 disabled。"
            "\n检查后执行 /skill enable 发布给新 Session。"
        )

    async def _inspect(self, rest: str) -> str:
        skill_id = self._required_id(rest, "inspect")
        inspection = await self._service.inspect(skill_id)
        return json.dumps({
            "record": inspection.record.to_dict(),
            "main_instruction_chars": inspection.main_instruction_chars,
            "files": [
                {"path": path, "bytes": size}
                for path, size in inspection.files
            ],
        }, ensure_ascii=False, indent=2)

    async def _test(self, rest: str) -> str:
        skill_id = self._required_id(rest, "test")
        inspection = await self._service.test_skill(skill_id)
        return (
            f"Skill `{skill_id}` 校验通过："
            f"{len(inspection.files)} files, "
            f"{inspection.main_instruction_chars} instruction chars"
        )

    async def _set_enabled(self, rest: str, enabled: bool) -> str:
        action = "enable" if enabled else "disable"
        skill_id = self._required_id(rest, action)
        record = await self._service.set_enabled(skill_id, enabled)
        state = "启用" if enabled else "停用"
        return f"已{state} Skill `{record.name}` (revision={record.revision})"

    async def _remove(self, rest: str) -> str:
        skill_id = self._required_id(rest, "remove")
        record = await self._service.remove(skill_id)
        return f"已卸载 Skill `{record.name}`"

    async def _check_update(self, rest: str) -> str:
        parts = rest.split(maxsplit=3)
        if not parts:
            raise SkillCommandError(
                "/skill check-update <id> [[local|url|github] <locator> [ref]]"
            )
        skill_id = parts[0]
        replacement = None
        if len(parts) > 1:
            if len(parts) < 3 or parts[1] not in {"local", "url", "github"}:
                raise SkillCommandError(
                    "/skill check-update <id> [[local|url|github] <locator> [ref]]"
                )
            replacement = SkillSourceRef(
                parts[1],
                parts[2],
                parts[3] if len(parts) == 4 else None,
            )
        candidate = await self._service.check_update(skill_id, replacement)
        return json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2)

    async def _update(self, rest: str) -> str:
        parts = rest.split()
        if len(parts) != 2:
            raise SkillCommandError("/skill update <id> <candidate_hash>")
        record = await self._service.update(parts[0], parts[1])
        return f"已更新 Skill `{record.name}` (revision={record.revision})"

    @staticmethod
    def _required_id(rest: str, action: str) -> str:
        skill_id = rest.strip()
        if not skill_id:
            raise SkillCommandError(f"/skill {action} <id>")
        return skill_id

    @staticmethod
    def _with_new_session(message: str) -> str:
        return f"{message}\n能力集合变化仅在新 Session 生效。"

    @staticmethod
    def _help() -> str:
        return (
            "Skill 命令：\n"
            "  /skill list\n"
            "  /skill install [local|url|github] <locator> [ref]\n"
            "  /skill inspect <id>\n"
            "  /skill test <id>\n"
            "  /skill enable <id>\n"
            "  /skill disable <id>\n"
            "  /skill remove <id>"
            "\n  /skill check-update <id> [[local|url|github] <locator> [ref]]"
            "\n  /skill update <id> <candidate_hash>"
            "\n  /skill reload"
        )
