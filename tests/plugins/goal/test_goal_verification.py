import json
import unittest

from core.tools_runtime.turn_evidence import TurnEvidence, ToolEvidence
from plugins.goal.capabilities import GoalJudgeCapability
from plugins.goal.submissions import JudgmentBuffer, JudgmentSubmission
from plugins.goal.goal import (
    CompletionContract,
    CompletionContractDraft,
    CompletionCriterion,
    CriterionAuthority,
    JudgmentDecision,
)
from plugins.goal.verification import (
    CommandRequirement,
    GoalVerification,
    WorkspaceRequirement,
)


def contract():
    return CompletionContract.initial(
        CompletionContractDraft(
            criteria=(
                CompletionCriterion(
                    "user-goal",
                    "测试通过且改动范围正确",
                    CriterionAuthority.USER,
                    ("真实测试结果", "真实工作区状态"),
                ),
            ),
            verification=GoalVerification(
                commands=(CommandRequirement(
                    "pytest",
                    workspace_root_id="project",
                ),),
                workspace=WorkspaceRequirement(
                    "project",
                    changed=True,
                    allowed_paths=("app.py",),
                ),
            ),
        )
    )


def evidence():
    return TurnEvidence(
        steps=(
            ToolEvidence(
                "call-1",
                "execute_command",
                json.dumps({"command": "python -m pytest"}),
                {
                    "ok": True,
                    "code": "COMMAND_COMPLETED",
                    "data": {
                        "timed_out": False,
                        "exit_code": 0,
                        "cwd": ".",
                        "workspace_membership": {
                            "root_id": "project",
                        },
                    },
                },
            ),
            ToolEvidence(
                "call-2",
                "get_changes",
                json.dumps({"path": "."}),
                {
                    "ok": True,
                    "data": {
                        "workspace_membership": {
                            "root_id": "project",
                        },
                        "changed": True,
                        "status": " M app.py",
                    },
                },
            ),
        )
    )


class GoalJudgeCapabilityTest(unittest.TestCase):
    def setUp(self):
        self.buffer = JudgmentBuffer()
        self.capability = GoalJudgeCapability(
            "goal-1",
            "完成目标",
            1,
            contract(),
            "Executor 声称完成",
            self.buffer,
        )

    def test_done_requires_real_structured_evidence(self):
        self.buffer.submit(
            JudgmentSubmission(
                JudgmentDecision.DONE,
                "全部通过",
                ("测试通过",),
            )
        )

        rejected = self.capability.check_final_candidate(TurnEvidence())
        accepted = self.capability.check_final_candidate(evidence())

        self.assertIn("缺少命令验收证据", rejected)
        self.assertIsNone(accepted)

    def test_judge_can_replace_rejected_done_with_continue(self):
        self.buffer.submit(
            JudgmentSubmission(
                JudgmentDecision.DONE,
                "误判完成",
                ("只有模型声明，没有真实证据",),
            )
        )
        self.assertIsNotNone(
            self.capability.check_final_candidate(TurnEvidence())
        )

        self.buffer.submit(
            JudgmentSubmission(
                JudgmentDecision.CONTINUE,
                "缺少实际验证",
                (),
            )
        )
        self.assertIsNone(
            self.capability.check_final_candidate(TurnEvidence())
        )


if __name__ == "__main__":
    unittest.main()
