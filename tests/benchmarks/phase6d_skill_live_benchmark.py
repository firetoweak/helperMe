from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import sys
import tempfile

from core.agent_workspace import AgentWorkspace
from core.composition import create_agent_application
from core.environment import (
    EnvironmentSelection,
    RootBinding,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from core.model_call.client import LLMClient
from core.model_call.config import load_app_config
from core.tools_runtime.progressive_skills import LOAD_SKILL, READ_SKILL_RESOURCE
from core.tools_runtime.turn_invocation import TurnInvocation
from plugins.skills.application import SkillApplicationService
from plugins.skills.summarizer import LlmSkillDiffSummarizer


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_NAME = "skill-benchmark-output.txt"


def _create_skill(source: Path) -> None:
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\n"
        "name: artifact-maker\n"
        "description: 按固定工作流生成并验证任务产物\n"
        "---\n\n"
        "必须严格按顺序执行：\n"
        "1. 调用 read_skill_resource 读取 references/guide.md。\n"
        "2. 在 Task Workspace cwd 调用 execute_command，执行 "
        "<skill-dir>/scripts/create.ps1，参数 OutputPath 为 "
        f"{OUTPUT_NAME}。\n"
        "3. 调用 get_changes 验证产物。\n"
        "不得把 cwd 改为 Skill Directory。\n",
        encoding="utf-8",
        newline="\n",
    )
    reference = source / "references" / "guide.md"
    reference.parent.mkdir()
    reference.write_text(
        f"产物路径必须是 Task Workspace 下的 {OUTPUT_NAME}，"
        "内容必须是 generated-from-phase6d-skill。\n",
        encoding="utf-8",
        newline="\n",
    )
    script = source / "scripts" / "create.ps1"
    script.parent.mkdir()
    script.write_text(
        "param([Parameter(Mandatory=$true)][string]$OutputPath)\n"
        "Set-Content -LiteralPath $OutputPath "
        "-Value 'generated-from-phase6d-skill'\n",
        encoding="utf-8",
        newline="\n",
    )


async def run() -> dict:
    config = load_app_config()
    agent_parent = Path(tempfile.mkdtemp(prefix="helperme-phase6d-agent-"))
    task_directory = Path(tempfile.mkdtemp(
        prefix=".phase6d-task-",
        dir=PROJECT_ROOT,
    ))
    try:
        workspace = AgentWorkspace(agent_parent / ".helperme")
        workspace.initialize()
        source = agent_parent / "source"
        _create_skill(source)
        llm_client = LLMClient(config.model)
        skills = SkillApplicationService(
            workspace,
            diff_summarizer=LlmSkillDiffSummarizer(
                llm_client,
                config.model.name,
            ),
        )
        installed = await skills.install_local(source)
        enabled = await skills.set_enabled(installed.name, True)

        application = create_agent_application(
            config.model.name,
            model_context_limit=config.runtime.model_context_limit,
            agent_workspace=workspace,
            workspace_roots={"project": PROJECT_ROOT},
            input_budget_ratio=config.runtime.input_budget_ratio,
            llm_client=llm_client,
            application_resources=(llm_client,),
            default_max_steps=config.runtime.max_steps,
            default_skill_provider=skills.skill_provider,
        )
        skills.bind_active_turn_guard(application.has_active_turns)
        view = WorkspaceViewSnapshot((RootBinding(
            "project",
            WorkspaceScope.TASK,
            PROJECT_ROOT,
        ),))
        selection = EnvironmentSelection(
            environment_id="local",
            workspace_view=view,
            cwd=str(task_directory),
        )

        async with application:
            session_id = application.create_session("phase6d-live")
            outcome = await application.start(
                session_id,
                "turn-1",
                (
                    "请使用 artifact-maker Skill 生成指定产物。"
                    "必须遵循 Skill 中的完整工作流并基于真实工具证据回答。"
                ),
                invocation=TurnInvocation(
                    skill_provider=skills.skill_provider,
                    environment_selection=selection,
                ),
            )

            (source / "SKILL.md").write_text(
                "---\n"
                "name: artifact-maker\n"
                "description: 按 v2 工作流生成并验证任务产物\n"
                "---\n\n"
                "v2 workflow: 仍需读取 reference、执行脚本并验证。\n",
                encoding="utf-8",
                newline="\n",
            )
            update_report = await skills.check_update("artifact-maker")
            frozen_hash = update_report.candidate.candidate_hash
            (source / "SKILL.md").write_text(
                "---\n"
                "name: artifact-maker\n"
                "description: drifted v3 workflow\n"
                "---\n\nsource drift after check-update\n",
                encoding="utf-8",
                newline="\n",
            )
            updated = await skills.update("artifact-maker", frozen_hash)

        output = task_directory / OUTPUT_NAME
        evidence_names = [
            item.name for item in outcome.result.evidence.steps
        ]
        output_content = (
            output.read_text(encoding="utf-8").strip()
            if output.is_file()
            else None
        )
        checks = {
            "installed_disabled_initially": installed.enabled is False,
            "enabled_revision_advanced": enabled.revision > installed.revision,
            "load_skill_called": LOAD_SKILL in evidence_names,
            "reference_read": READ_SKILL_RESOURCE in evidence_names,
            "script_executed": "execute_command" in evidence_names,
            "workspace_verified": "get_changes" in evidence_names,
            "output_exists": output.is_file(),
            "output_content_correct": (
                output_content == "generated-from-phase6d-skill"
            ),
            "semantic_update_summary_generated": bool(
                update_report.semantic_summary
            ),
            "machine_diff_kept": update_report.candidate.diff.changed,
            "frozen_candidate_applied": updated.content_hash == frozen_hash,
            "source_drift_not_applied": updated.description.startswith("按 v2"),
        }
        return {
            "passed": all(checks.values()),
            "checks": checks,
            "turn_status": outcome.result.status.value,
            "evidence_tools": evidence_names,
            "output_content": output_content,
            "answer": outcome.result.answer,
            "update_summary": update_report.semantic_summary,
            "update_summary_error": update_report.summary_error,
        }
    finally:
        shutil.rmtree(agent_parent, ignore_errors=True)
        shutil.rmtree(task_directory, ignore_errors=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    result = asyncio.run(run())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
