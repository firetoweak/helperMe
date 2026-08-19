from __future__ import annotations

from typing import Literal, Mapping
from uuid import uuid4

from pydantic import BaseModel

from core.approval import ApprovalExecution, ApprovalRequest
from core.tool_registry import PydanticParameters, ToolSpec
from plugins.skills.application import SkillApplicationService
from plugins.skills.models import SkillSourceRef


SKILL_INSTALL_ACTION = "skill.install"
SKILL_ENABLE_ACTION = "skill.enable"
PROPOSE_SKILL_INSTALL = "propose_skill_install"
PROPOSE_SKILL_ENABLE = "propose_skill_enable"


class SkillInstallProposalInput(BaseModel):
    source_kind: Literal["local", "github", "url"]
    locator: str
    requested_ref: str | None = None


def create_skill_install_proposal_spec(
    service: SkillApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: SkillInstallProposalInput,
    ) -> ApprovalRequest:
        candidate = await service.prepare_install(SkillSourceRef(
            input_data.source_kind,
            input_data.locator,
            input_data.requested_ref,
        ))
        return ApprovalRequest(
            id=f"approval-{uuid4().hex}",
            action=SKILL_INSTALL_ACTION,
            payload={
                "skill_id": candidate.skill_id,
                "content_hash": candidate.content_hash,
            },
            summary=(
                f"准备安装 Skill `{candidate.skill_id}`\n"
                f"描述：{candidate.description}\n"
                f"来源：{candidate.source.kind} {candidate.source.locator}\n"
                f"解析引用：{candidate.resolved_ref}\n"
                f"Content hash：{candidate.content_hash}\n"
                "安装后保持 disabled，不会立即进入模型能力目录。"
            ),
            risk=(
                "Skill 包可包含外部指令和脚本；"
                "当前只证明结构、路径、大小与 hash 符合契约，"
                "不证明内容安全。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_SKILL_INSTALL,
        description=(
            "当用户要求从 local/GitHub/URL 安装 Skill 时，"
            "获取并冻结确定候选，然后提交用户审批。"
            "信息不足时先询问；本工具必须单独调用。"
        ),
        parameters=PydanticParameters(SkillInstallProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class SkillInstallApprovalHandler:
    action = SKILL_INSTALL_ACTION

    def __init__(self, service: SkillApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ApprovalExecution:
        record = await self.service.install_frozen(
            str(payload["skill_id"]),
            str(payload["content_hash"]),
        )
        return ApprovalExecution(
            succeeded=True,
            message=(
                f"Skill `{record.name}` 已安装为 disabled。"
                "请 inspect/test 后显式 enable。"
            ),
            data={
                "skill_id": record.name,
                "revision": record.revision,
                "content_hash": record.content_hash,
                "enabled": record.enabled,
            },
        )


class SkillEnableProposalInput(BaseModel):
    skill_id: str


def create_skill_enable_proposal_spec(
    service: SkillApplicationService,
) -> ToolSpec:
    async def propose(input_data: SkillEnableProposalInput) -> ApprovalRequest:
        inspection = await service.test_skill(input_data.skill_id)
        record = inspection.record
        if record.enabled:
            raise ValueError(f"Skill 已启用: {record.name}")
        return ApprovalRequest(
            id=f"approval-{uuid4().hex}",
            action=SKILL_ENABLE_ACTION,
            payload={
                "skill_id": record.name,
                "expected_revision": record.revision,
                "expected_hash": record.content_hash,
            },
            summary=(
                f"准备启用 Skill `{record.name}`\n"
                f"Revision：{record.revision}\n"
                f"Content hash：{record.content_hash}\n"
                f"主指令长度：{inspection.main_instruction_chars} chars"
            ),
            risk=(
                "启用后，该 Skill 的外部指令会从下一个 Turn 进入目录；"
                "其脚本仍只在 Agent 显式调用命令时执行。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_SKILL_ENABLE,
        description=(
            "对已 inspect/test 且 disabled 的 Skill 提交启用审批。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(SkillEnableProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class SkillEnableApprovalHandler:
    action = SKILL_ENABLE_ACTION

    def __init__(self, service: SkillApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ApprovalExecution:
        record = await self.service.enable_frozen(
            str(payload["skill_id"]),
            int(payload["expected_revision"]),
            str(payload["expected_hash"]),
        )
        return ApprovalExecution(
            succeeded=True,
            message=(
                f"Skill `{record.name}` 已启用。"
                "最新目录将从下一个 Turn 生效。"
            ),
            data={
                "skill_id": record.name,
                "revision": record.revision,
                "enabled": record.enabled,
            },
        )
