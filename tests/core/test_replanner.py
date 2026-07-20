import json
import unittest

from core.planning.replanner import (
    InvalidReplanResponse,
    ReplanDecision,
    parse_replan_response,
)


class ReplannerTest(unittest.TestCase):
    def test_parse_keep_decision(self):
        decision = parse_replan_response(
            '{"action": "keep", "reason": "失败不影响原计划"}'
        )

        self.assertEqual(
            decision,
            ReplanDecision(
                action="keep",
                reason="失败不影响原计划",
                steps=[],
            ),
        )

    def test_parse_revise_decision_trims_reason_and_steps(self):
        decision = parse_replan_response(
            """
            {
                "action": "revise",
                "reason": "  原接口不存在  ",
                "steps": ["  检查本地数据  ", "验证结果"]
            }
            """
        )

        self.assertEqual(
            decision,
            ReplanDecision(
                action="revise",
                reason="原接口不存在",
                steps=["检查本地数据", "验证结果"],
            ),
        )

    def test_invalid_json_or_non_object_fails(self):
        for content in ("不是 JSON", "[]", '"keep"'):
            with self.subTest(content=content):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(content)

    def test_unknown_or_missing_action_fails(self):
        for content in (
            '{"reason": "缺少 action"}',
            '{"action": "retry", "reason": "未知 action"}',
        ):
            with self.subTest(content=content):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(content)

    def test_missing_or_blank_reason_fails(self):
        for content in (
            '{"action": "keep"}',
            '{"action": "keep", "reason": "   "}',
            '{"action": "keep", "reason": 123}',
        ):
            with self.subTest(content=content):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(content)

    def test_keep_with_steps_fails(self):
        with self.assertRaises(InvalidReplanResponse):
            parse_replan_response(
                """
                {
                    "action": "keep",
                    "reason": "继续原计划",
                    "steps": ["不应提供新步骤"]
                }
                """
            )

    def test_revise_requires_one_to_six_non_empty_steps(self):
        invalid_steps = (
            None,
            [],
            ["步骤1", "步骤2", "步骤3", "步骤4", "步骤5", "步骤6", "步骤7"],
            ["有效步骤", "   "],
            ["有效步骤", 123],
        )

        for steps in invalid_steps:
            if steps is None:
                content = '{"action": "revise", "reason": "需要修改"}'
            else:
                content = json.dumps(
                    {
                        "action": "revise",
                        "reason": "需要修改",
                        "steps": steps,
                    },
                    ensure_ascii=False,
                )

            with self.subTest(steps=steps):
                with self.assertRaises(InvalidReplanResponse):
                    parse_replan_response(content)


if __name__ == "__main__":
    unittest.main()
