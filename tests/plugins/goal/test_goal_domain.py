import unittest

from plugins.goal.goal import (
    CompletionContract,
    CompletionContractDraft,
    CompletionCriterion,
    CriterionAuthority,
    Goal,
    GoalJudgment,
    GoalStatus,
    JudgmentDecision,
)


def contract_draft(*criteria):
    return CompletionContractDraft(tuple(criteria))


def criterion(
    criterion_id="user-goal",
    description="完成用户目标",
    authority=CriterionAuthority.USER,
):
    return CompletionCriterion(
        criterion_id,
        description,
        authority,
        ("Judge 独立检查最终结果",),
    )


class CompletionContractTest(unittest.TestCase):
    def test_contract_requires_a_user_criterion(self):
        with self.assertRaisesRegex(ValueError, "user criterion"):
            contract_draft(
                criterion(
                    "inferred",
                    "补充标准",
                    CriterionAuthority.INFERRED,
                )
            )

    def test_revision_may_change_inferred_but_not_user_criteria(self):
        user = criterion()
        initial = CompletionContract.initial(
            contract_draft(
                user,
                criterion(
                    "quality",
                    "旧推导标准",
                    CriterionAuthority.INFERRED,
                ),
            )
        )

        revised = initial.revise(
            contract_draft(
                user,
                criterion(
                    "quality-v2",
                    "新推导标准",
                    CriterionAuthority.INFERRED,
                ),
            )
        )

        self.assertEqual(revised.version, 2)
        with self.assertRaisesRegex(ValueError, "user criteria"):
            initial.revise(
                contract_draft(
                    criterion(description="降低后的用户目标")
                )
            )


class GoalLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.goal = Goal(
            "goal-1",
            "完成目标",
            CompletionContract.initial(contract_draft(criterion())),
            max_turns=2,
        )

    def judge(self, decision, reason="验收结果"):
        turn = self.goal.turns[-1]
        self.goal.record_judgment(
            GoalJudgment(
                turn.index,
                turn.judge_run_id,
                decision,
                reason,
                ("独立验证证据",),
            )
        )

    def test_continue_opens_next_turn_and_done_completes_goal(self):
        self.goal.start_turn("executor-1")
        self.goal.begin_judgment("executor-1", "judge-1", "第一轮完成")
        self.judge(JudgmentDecision.CONTINUE, "仍缺回归测试")

        self.assertEqual(self.goal.status, GoalStatus.ACTIVE)
        self.assertEqual(self.goal.latest_feedback, "仍缺回归测试")

        self.goal.start_turn("executor-2")
        self.goal.begin_judgment("executor-2", "judge-2", "第二轮完成")
        self.judge(JudgmentDecision.DONE)

        self.assertEqual(self.goal.status, GoalStatus.COMPLETED)
        self.assertEqual(self.goal.turn_count, 2)

    def test_continue_on_last_turn_exhausts_goal(self):
        for index in (1, 2):
            run_id = f"executor-{index}"
            self.goal.start_turn(run_id)
            self.goal.begin_judgment(
                run_id,
                f"judge-{index}",
                f"第 {index} 轮",
            )
            self.judge(JudgmentDecision.CONTINUE)

        self.assertEqual(self.goal.status, GoalStatus.EXHAUSTED)

    def test_pause_requires_explicit_resume(self):
        self.goal.start_turn("executor-1")
        self.goal.begin_judgment("executor-1", "judge-1", "无法继续")
        self.judge(JudgmentDecision.PAUSE, "用户标准客观不可满足")

        self.assertEqual(self.goal.status, GoalStatus.PAUSED)
        self.goal.resume()
        self.assertEqual(self.goal.status, GoalStatus.ACTIVE)

    def test_interrupted_executor_does_not_consume_max_turns(self):
        self.goal.start_turn("executor-1")
        self.goal.interrupt_turn("executor-1", "用户暂停")

        self.assertEqual(self.goal.turn_count, 0)
        self.goal.resume()
        resumed = self.goal.start_turn("executor-2")
        self.assertEqual(resumed.index, 1)


if __name__ == "__main__":
    unittest.main()
