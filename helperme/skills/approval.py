from __future__ import annotations

from typing import Literal, Mapping, cast
from uuid import uuid4

from pydantic import BaseModel, model_validator

from helperme.tools.control import ControlApprovalExecution, ControlApprovalRequest
from helperme.tools.spec import PydanticParameters, ToolSpec
from helperme.skills.application import SkillApplicationService
from helperme.skills.models import SkillSourceRef
from helperme.skills.sources import SkillSourceError
from helperme.skills.errors import SkillAlreadyInstalledError, SkillInputError


SKILL_INSTALL_ACTION = "skill.install"
SKILL_ENABLE_ACTION = "skill.enable"
SKILL_UPDATE_ACTION = "skill.update"
SKILL_REPAIR_ACTION = "skill.repair"
PROPOSE_SKILL_INSTALL = "propose_skill_install"
PROPOSE_SKILL_ENABLE = "propose_skill_enable"
PROPOSE_SKILL_UPDATE = "propose_skill_update"
PROPOSE_SKILL_REPAIR = "propose_skill_repair"


class SkillInstallProposalInput(BaseModel):
    source_kind: Literal["local", "github", "url"]
    locator: str
    requested_ref: str | None = None


def create_skill_install_proposal_spec(
    service: SkillApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: SkillInstallProposalInput,
    ) -> ControlApprovalRequest | dict:
        try:
            candidate = await service.prepare_install(SkillSourceRef(
                input_data.source_kind,
                input_data.locator,
                input_data.requested_ref,
            ))
        except (SkillSourceError, SkillAlreadyInstalledError) as exc:
            return {
                "ok": False,
                "code": (
                    "SKILL_ALREADY_INSTALLED"
                    if isinstance(exc, SkillAlreadyInstalledError)
                    else "SKILL_SOURCE_ERROR"
                ),
                "data": {
                    "source_kind": input_data.source_kind,
                    "locator": input_data.locator,
                },
                "error": str(exc),
                "hint": (
                    "检查已安装目录；更新 Skill 应走独立更新流程。"
                    if isinstance(exc, SkillAlreadyInstalledError)
                    else "检查网络、来源地址或本地路径后重试。"
                ),
            }
        return ControlApprovalRequest(
            id=f"approval-{uuid4().hex}",
            action=SKILL_INSTALL_ACTION,
            payload={
                "skill_id": candidate.skill_id,
                "content_hash": candidate.content_hash,
                "source": candidate.source.to_dict(),
                "resolved_ref": candidate.resolved_ref,
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

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        if set(payload) != {
            "skill_id",
            "content_hash",
            "source",
            "resolved_ref",
        }:
            raise SkillInputError("Skill install approval payload 字段不匹配")
        source = payload["source"]
        if not isinstance(source, Mapping):
            raise SkillInputError("Skill install approval source 必须是 object")
        skill_id = payload["skill_id"]
        content_hash = payload["content_hash"]
        resolved_ref = payload["resolved_ref"]
        if any(
            type(value) is not str
            for value in (skill_id, content_hash, resolved_ref)
        ):
            raise SkillInputError("Skill install approval identity 类型无效")
        record = await self.service.install_frozen(
            cast(str, skill_id),
            cast(str, content_hash),
            SkillSourceRef.from_dict(dict(source)),
            cast(str, resolved_ref),
        )
        return ControlApprovalExecution(
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
    async def propose(input_data: SkillEnableProposalInput) -> ControlApprovalRequest:
        inspection = await service.test_skill(input_data.skill_id)
        record = inspection.record
        if record.enabled:
            raise ValueError(f"Skill 已启用: {record.name}")
        return ControlApprovalRequest(
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
                "启用后，该 Skill 的外部指令会从下一个 Step 进入目录；"
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

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        if set(payload) != {
            "skill_id",
            "expected_revision",
            "expected_hash",
        }:
            raise SkillInputError("Skill enable approval payload 字段不匹配")
        skill_id = payload["skill_id"]
        expected_revision = payload["expected_revision"]
        expected_hash = payload["expected_hash"]
        if (
            type(skill_id) is not str
            or type(expected_revision) is not int
            or type(expected_hash) is not str
        ):
            raise SkillInputError("Skill enable approval payload 类型无效")
        record = await self.service.enable_frozen(
            skill_id,
            expected_revision,
            expected_hash,
        )
        return ControlApprovalExecution(
            succeeded=True,
            message=(
                f"Skill `{record.name}` 已启用。"
                "最新目录将从下一个 Step 生效。"
            ),
            data={
                "skill_id": record.name,
                "revision": record.revision,
                "enabled": record.enabled,
            },
        )


class SkillUpdateProposalInput(BaseModel):
    skill_id: str
    source_kind: Literal["local", "github", "url"] | None = None
    locator: str | None = None
    requested_ref: str | None = None

    @model_validator(mode="after")
    def validate_replacement(self) -> "SkillUpdateProposalInput":
        if (self.source_kind is None) != (self.locator is None):
            raise ValueError("replacement source_kind/locator 必须同时提供")
        if self.source_kind is None and self.requested_ref is not None:
            raise ValueError("requested_ref 需要 replacement source")
        return self

    def replacement(self) -> SkillSourceRef | None:
        if self.source_kind is None:
            return None
        return SkillSourceRef(
            self.source_kind,
            cast(str, self.locator),
            self.requested_ref,
        )


def create_skill_update_proposal_spec(
    service: SkillApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: SkillUpdateProposalInput,
    ) -> ControlApprovalRequest | dict:
        try:
            report = await service.check_update(
                input_data.skill_id,
                input_data.replacement(),
            )
        except (SkillInputError, SkillSourceError) as exc:
            return {
                "ok": False,
                "code": "SKILL_UPDATE_CHECK_FAILED",
                "data": {"skill_id": input_data.skill_id},
                "error": str(exc),
                "hint": None,
            }
        candidate = report.candidate
        if not candidate.diff.changed:
            return {
                "ok": True,
                "code": "SKILL_ALREADY_CURRENT",
                "data": report.to_dict(),
                "error": None,
                "hint": None,
            }
        return ControlApprovalRequest(
            id=f"approval-{uuid4().hex}",
            action=SKILL_UPDATE_ACTION,
            payload={
                "skill_id": candidate.skill_id,
                "candidate_hash": candidate.candidate_hash,
            },
            summary=(
                f"准备{candidate.operation} Skill `{candidate.skill_id}`\n"
                f"Candidate hash：{candidate.candidate_hash}\n"
                f"Diff：{candidate.diff.to_dict()}\n"
                f"语义概括：{report.semantic_summary or report.summary_error}"
            ),
            risk=(
                "批准后将以冻结候选原子替换已安装 Skill；"
                "启用状态保持不变，revision 必须仍与检查时一致。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_SKILL_UPDATE,
        description=(
            "为健康的已安装 Skill 检查来源更新并冻结候选，提交更新审批。"
            "诊断显示安装包损坏时不要调用，应改用 propose_skill_repair。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(SkillUpdateProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class SkillUpdateApprovalHandler:
    action = SKILL_UPDATE_ACTION

    def __init__(self, service: SkillApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        if set(payload) != {"skill_id", "candidate_hash"}:
            raise SkillInputError("Skill update approval payload 字段不匹配")
        skill_id = payload["skill_id"]
        candidate_hash = payload["candidate_hash"]
        if type(skill_id) is not str or type(candidate_hash) is not str:
            raise SkillInputError("Skill update approval payload 类型无效")
        try:
            record = await self.service.update(skill_id, candidate_hash)
        except SkillInputError as exc:
            return ControlApprovalExecution(
                False,
                f"Skill `{skill_id}` 更新未执行：{exc}",
            )
        return ControlApprovalExecution(
            True,
            f"Skill `{record.name}` 已更新 (revision={record.revision})。",
            {"skill_id": record.name, "revision": record.revision},
        )


class SkillRepairProposalInput(BaseModel):
    skill_id: str


def create_skill_repair_proposal_spec(
    service: SkillApplicationService,
) -> ToolSpec:
    async def propose(
        input_data: SkillRepairProposalInput,
    ) -> ControlApprovalRequest | dict:
        try:
            record, candidate = await service.prepare_repair(
                input_data.skill_id,
            )
        except (SkillInputError, SkillSourceError) as exc:
            return {
                "ok": False,
                "code": "SKILL_REPAIR_PREPARE_FAILED",
                "data": {"skill_id": input_data.skill_id},
                "error": str(exc),
                "hint": None,
            }
        return ControlApprovalRequest(
            id=f"approval-{uuid4().hex}",
            action=SKILL_REPAIR_ACTION,
            payload={
                "skill_id": record.name,
                "candidate_hash": candidate.content_hash,
                "source": candidate.source.to_dict(),
                "resolved_ref": candidate.resolved_ref,
                "expected_revision": record.revision,
                "expected_content_hash": record.content_hash,
            },
            summary=(
                f"准备修复 Skill `{record.name}`\n"
                f"Revision：{record.revision}\n"
                f"登记 hash：{record.content_hash}\n"
                f"冻结来源：{candidate.source.kind} {candidate.source.locator}"
            ),
            risk=(
                "批准后只恢复与 Registry 登记 hash 完全相同的冻结内容；"
                "来源内容变化时拒绝修复，不会把修复暗中变成更新。"
            ),
        )

    return ToolSpec(
        name=PROPOSE_SKILL_REPAIR,
        description=(
            "在 inspect/test 已证明已安装 Skill 包损坏或丢失后，"
            "从登记来源冻结同 hash 内容并提交修复审批。"
            "本工具必须单独调用。"
        ),
        parameters=PydanticParameters(SkillRepairProposalInput),
        handler=propose,
        control_boundary=True,
        exclusive_batch=True,
    )


class SkillRepairApprovalHandler:
    action = SKILL_REPAIR_ACTION

    def __init__(self, service: SkillApplicationService) -> None:
        self.service = service

    async def execute(self, payload: Mapping[str, object]) -> ControlApprovalExecution:
        expected = {
            "skill_id",
            "candidate_hash",
            "source",
            "resolved_ref",
            "expected_revision",
            "expected_content_hash",
        }
        if set(payload) != expected:
            raise SkillInputError("Skill repair approval payload 字段不匹配")
        source = payload["source"]
        if not isinstance(source, Mapping):
            raise SkillInputError("Skill repair source 必须是 object")
        if (
            type(payload["skill_id"]) is not str
            or type(payload["candidate_hash"]) is not str
            or type(payload["resolved_ref"]) is not str
            or type(payload["expected_revision"]) is not int
            or type(payload["expected_content_hash"]) is not str
        ):
            raise SkillInputError("Skill repair approval payload 类型无效")
        try:
            record = await self.service.repair_frozen(
                cast(str, payload["skill_id"]),
                cast(str, payload["candidate_hash"]),
                SkillSourceRef.from_dict(dict(source)),
                cast(str, payload["resolved_ref"]),
                expected_revision=cast(int, payload["expected_revision"]),
                expected_content_hash=cast(
                    str,
                    payload["expected_content_hash"],
                ),
            )
        except SkillInputError as exc:
            return ControlApprovalExecution(
                False,
                f"Skill repair 未执行：{exc}",
            )
        return ControlApprovalExecution(
            True,
            f"Skill `{record.name}` 已修复 (revision={record.revision})。",
            {"skill_id": record.name, "revision": record.revision},
        )
