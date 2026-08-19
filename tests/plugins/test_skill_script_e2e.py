import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.agent_workspace import AgentWorkspace
from core.context import ContextState
from core.environment import (
    EnvironmentBinding,
    PermissionBinding,
    RootBinding,
    RuntimeAttachment,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from core.runtime_modes import PlainMode
from core.tools_runtime.progressive_skills import LOAD_SKILL, READ_SKILL_RESOURCE
from core.tools_runtime.turn_invocation import TurnInvocation
from core.tools_runtime.turn_runtime import TurnRuntime, TurnStatus
from plugins.skills.application import SkillApplicationService
from tests.core.llm_test_support import (
    call_result,
    context_preparation_service,
    model_call_service,
    runtime_tool_dependencies,
)
from tests.plugins.test_skill_package import write_skill
from tools import create_environment_tool_specs
from tools.powershell_runner import PowerShellCommandRunner


POWERSHELL = shutil.which("powershell.exe")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RecordingClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, messages, model, tools=None):
        self.requests.append({"messages": messages, "tools": tools or []})
        return call_result(self.responses.pop(0))


@unittest.skipUnless(POWERSHELL, "需要 Windows PowerShell")
class SkillScriptEndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def test_loaded_skill_reads_reference_and_runs_script_from_task_cwd(self):
        with tempfile.TemporaryDirectory() as agent_directory, tempfile.TemporaryDirectory(
            dir=PROJECT_ROOT
        ) as task_directory:
            task_root = Path(task_directory)
            workspace = AgentWorkspace(Path(agent_directory) / ".helperme")
            workspace.initialize()
            source = Path(agent_directory) / "source"
            write_skill(
                source,
                name="artifact-maker",
                description="Create a verified task artifact",
                body=(
                    "\nRead references/guide.md, then run "
                    "<skill-dir>/scripts/create.ps1 from the Task Workspace cwd.\n"
                ),
            )
            reference = source / "references" / "guide.md"
            reference.parent.mkdir()
            reference.write_text("Output must be result.txt", encoding="utf-8")
            script = source / "scripts" / "create.ps1"
            script.parent.mkdir()
            script.write_text(
                "param([string]$OutputPath)\n"
                "Set-Content -LiteralPath $OutputPath "
                "-Value 'generated-from-skill'\n",
                encoding="utf-8",
            )
            service = SkillApplicationService(workspace)
            await service.install_local(source)
            await service.set_enabled("artifact-maker", True)
            provider = service.skill_provider
            skill_dir = workspace.skills_root / "packages" / "artifact-maker"
            command = (
                f"& '{skill_dir / 'scripts' / 'create.ps1'}' "
                "-OutputPath 'result.txt'"
            )
            client = RecordingClient([
                LLMResponse(calls=(ToolCall(
                    "load-1",
                    LOAD_SKILL,
                    '{"skill_id":"artifact-maker"}',
                ),)),
                LLMResponse(calls=(ToolCall(
                    "read-1",
                    READ_SKILL_RESOURCE,
                    json.dumps({
                        "skill_id": "artifact-maker",
                        "relative_path": "references/guide.md",
                    }),
                ),)),
                LLMResponse(calls=(ToolCall(
                    "command-1",
                    "execute_command",
                    json.dumps({
                        "command": command,
                        "workspace_effect": "may_write",
                    }),
                ),)),
                LLMResponse(calls=(ToolCall(
                    "verify-1",
                    "get_changes",
                    '{"path":"."}',
                ),)),
                LLMResponse(content="artifact created and verified"),
            ])
            view = WorkspaceViewSnapshot((RootBinding(
                "project",
                WorkspaceScope.TASK,
                PROJECT_ROOT,
            ),))
            runner = PowerShellCommandRunner()
            binding = EnvironmentBinding(
                environment_id="local-test",
                workspace_view=view,
                permission_binding=PermissionBinding.read_write(view),
                cwd=task_root,
                shell_name="powershell",
                shell_path=runner.executable,
                runtime_attachment=RuntimeAttachment(
                    environment_instance_id="local-test",
                    command_executor=runner,
                ),
            )
            conversation = Conversation()
            conversation.set_system_prompt("system")
            runtime = TurnRuntime(
                model_call_service(client),
                "test-model",
                PlainMode(),
                context_preparation_service(),
                environment_tool_factory=create_environment_tool_specs,
                **runtime_tool_dependencies(),
            )

            result = await runtime.run(
                conversation,
                "create artifact",
                context_state=ContextState(),
                invocation=TurnInvocation(
                    skill_provider=provider,
                    environment_binding=binding,
                ),
            )

            output = task_root / "result.txt"
            self.assertEqual(result.status, TurnStatus.COMPLETED)
            self.assertEqual(
                output.read_text(encoding="utf-8").strip(),
                "generated-from-skill",
            )
            self.assertEqual(
                [step.name for step in result.evidence.steps],
                [
                    LOAD_SKILL,
                    READ_SKILL_RESOURCE,
                    "execute_command",
                    "get_changes",
                ],
            )
            command_result = result.evidence.by_name("execute_command")[0].result
            self.assertEqual(command_result["code"], "COMMAND_COMPLETED")
            self.assertEqual(command_result["data"]["cwd"], task_root.name)
            self.assertIn(str(skill_dir), command)


if __name__ == "__main__":
    unittest.main()
