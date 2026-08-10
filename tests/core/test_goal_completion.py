import json
import unittest

from core.goals import (
    CommandRequirement,
    CompletionGate,
    TaskVerification,
    WorkspaceRequirement,
)
from core.tools_runtime import RunEvidence, ToolEvidence, WorkspaceBaseline


def command_evidence(
    call_id: str,
    command: str,
    exit_code: int,
) -> ToolEvidence:
    return ToolEvidence(
        call_id=call_id,
        name="execute_command",
        arguments=json.dumps(
            {
                "root": "workspace",
                "cwd": ".",
                "command": command,
            }
        ),
        result={
            "ok": True,
            "code": "COMMAND_COMPLETED",
            "data": {
                "exit_code": exit_code,
                "timed_out": False,
            },
        },
    )


class CompletionGateTest(unittest.TestCase):
    def setUp(self):
        self.gate = CompletionGate()

    def test_natural_language_criteria_without_contract_is_not_accepted(self):
        review = self.gate.review(None, "测试与构建通过", RunEvidence())

        self.assertFalse(review.accepted)
        self.assertIn("verification contract", review.reason)

    def test_command_requirement_uses_real_exit_code(self):
        verification = TaskVerification(
            commands=(CommandRequirement("python -m unittest"),)
        )
        failed = RunEvidence(
            (command_evidence("call-1", "python -m unittest -v", 1),)
        )
        passed = RunEvidence(
            (command_evidence("call-2", "python -m unittest -v", 0),)
        )

        self.assertFalse(
            self.gate.review(verification, "测试通过", failed).accepted
        )
        self.assertTrue(
            self.gate.review(verification, "测试通过", passed).accepted
        )

    def test_diagnostic_command_can_require_execution_with_any_exit_code(self):
        verification = TaskVerification(
            commands=(
                CommandRequirement(
                    "python scripts/build.py",
                    expected_exit_codes=None,
                ),
            )
        )
        failed_as_expected = RunEvidence(
            (command_evidence("call-1", "python scripts/build.py", 2),)
        )

        review = self.gate.review(
            verification,
            "实际执行预期失败的构建命令",
            failed_as_expected,
        )

        self.assertTrue(review.accepted)

    def test_workspace_requirement_rejects_unexpected_paths(self):
        verification = TaskVerification(
            workspace=WorkspaceRequirement(
                root="workspace",
                changed=True,
                allowed_paths=("calculator.py",),
            )
        )
        evidence = RunEvidence(
            (
                ToolEvidence(
                    call_id="changes-1",
                    name="get_changes",
                    arguments='{"root":"workspace"}',
                    result={
                        "ok": True,
                        "code": "CHANGES_READ",
                        "data": {
                            "root": "workspace",
                            "changed": True,
                            "status": " M calculator.py\n?? secret.txt\n",
                            "diff": "",
                        },
                    },
                ),
            )
        )

        review = self.gate.review(verification, "限制修改范围", evidence)

        self.assertFalse(review.accepted)
        self.assertIn("secret.txt", review.reason)

    def test_workspace_unchanged_requires_get_changes_evidence(self):
        verification = TaskVerification(
            workspace=WorkspaceRequirement(root="workspace", changed=False)
        )

        missing = self.gate.review(verification, "仓库无改动", RunEvidence())
        present = self.gate.review(
            verification,
            "仓库无改动",
            RunEvidence(
                (
                    ToolEvidence(
                        "changes-1",
                        "get_changes",
                        '{"root":"workspace"}',
                        {
                            "ok": True,
                            "code": "CHANGES_READ",
                            "data": {
                                "root": "workspace",
                                "changed": False,
                                "status": "",
                                "diff": "",
                            },
                        },
                    ),
                )
            ),
        )

        self.assertFalse(missing.accepted)
        self.assertTrue(present.accepted)

    def test_allowed_paths_are_checked_against_run_baseline(self):
        verification = TaskVerification(
            workspace=WorkspaceRequirement(
                root="workspace",
                changed=True,
                allowed_paths=("scripts/build.py",),
            )
        )
        evidence = RunEvidence(
            steps=(
                ToolEvidence(
                    "changes-1",
                    "get_changes",
                    '{"root":"workspace"}',
                    {
                        "ok": True,
                        "code": "CHANGES_READ",
                        "data": {
                            "root": "workspace",
                            "changed": True,
                            "status": (
                                " M calculator.py\n?? scripts/build.py\n"
                            ),
                            "diff": "",
                        },
                    },
                ),
            ),
            workspace_baselines=(
                WorkspaceBaseline(
                    "workspace",
                    {
                        "ok": True,
                        "code": "CHANGES_READ",
                        "data": {
                            "root": "workspace",
                            "changed": True,
                            "status": " M calculator.py\n",
                            "diff": "",
                        },
                    },
                ),
            ),
        )

        review = self.gate.review(verification, "只新增构建脚本", evidence)

        self.assertTrue(review.accepted)


if __name__ == "__main__":
    unittest.main()
