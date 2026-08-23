from __future__ import annotations

from pydantic import BaseModel

from helperme.tools.spec import EmptyInput, PydanticParameters, ToolSpec
from helperme.skills.application import SkillApplicationService
from helperme.skills.errors import SkillInputError, SkillNotFoundError


LIST_INSTALLED_SKILLS = "list_installed_skills"
INSPECT_INSTALLED_SKILL = "inspect_installed_skill"
TEST_INSTALLED_SKILL = "test_installed_skill"


class SkillIdInput(BaseModel):
    skill_id: str


def create_skill_management_specs(
    service: SkillApplicationService,
) -> tuple[ToolSpec, ...]:
    async def list_skills(_input: EmptyInput):
        records = await service.list_skills()
        return {
            "ok": True,
            "code": "SKILLS_LISTED",
            "data": {
                "skills": [record.to_dict() for record in records],
            },
        }

    async def inspect(input_data: SkillIdInput):
        try:
            result = await service.inspect(input_data.skill_id)
        except SkillNotFoundError:
            return _not_found(input_data.skill_id)
        except SkillInputError as exc:
            return _invalid(input_data.skill_id, exc)
        return {
            "ok": True,
            "code": "SKILL_INSPECTED",
            "data": {
                "record": result.record.to_dict(),
                "main_instruction_chars": result.main_instruction_chars,
                "files": [
                    {"path": path, "bytes": size}
                    for path, size in result.files
                ],
            },
        }

    async def test_skill(input_data: SkillIdInput):
        try:
            result = await service.test_skill(input_data.skill_id)
        except SkillNotFoundError:
            return _not_found(input_data.skill_id)
        except SkillInputError as exc:
            return _invalid(input_data.skill_id, exc)
        return {
            "ok": True,
            "code": "SKILL_TEST_PASSED",
            "data": {
                "skill_id": result.record.name,
                "revision": result.record.revision,
                "content_hash": result.record.content_hash,
                "enabled": result.record.enabled,
            },
        }

    return (
        ToolSpec(
            LIST_INSTALLED_SKILLS,
            "列出已安装 Skill 的管理目录，包含 disabled 项。",
            PydanticParameters(EmptyInput),
            list_skills,
        ),
        ToolSpec(
            INSPECT_INSTALLED_SKILL,
            "检查已安装 Skill 的登记信息、文件 manifest 和主指令大小。",
            PydanticParameters(SkillIdInput),
            inspect,
        ),
        ToolSpec(
            TEST_INSTALLED_SKILL,
            "重新校验已安装 Skill 的身份、Frontmatter、路径、大小与 hash。",
            PydanticParameters(SkillIdInput),
            test_skill,
        ),
    )


def _not_found(skill_id: str) -> dict:
    return {
        "ok": False,
        "code": "SKILL_NOT_INSTALLED",
        "data": {"skill_id": skill_id},
        "error": f"Skill 未安装: {skill_id}",
        "hint": "先调用 list_installed_skills 查看包含 disabled 项的管理目录。",
    }


def _invalid(skill_id: str, exc: SkillInputError) -> dict:
    return {
        "ok": False,
        "code": "SKILL_INVALID",
        "data": {"skill_id": skill_id},
        "error": str(exc),
        "hint": "检查已安装 Skill 包和 Registry 后重试。",
    }
