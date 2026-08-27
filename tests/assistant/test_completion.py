from __future__ import annotations

import unittest

from helperme.assistant.completion.criteria import (
    CriteriaCommitted,
    CriteriaSource,
    JudgmentVerdict,
    SessionFacts,
    classify_user_intent,
    criteria_from_fact,
    inferred_from_facts,
    judgment_from_fact,
)
from helperme.assistant.completion.judgment import parse_judgment
from helperme.runtime import DomainFactCommitted


class CriteriaAndJudgeTest(unittest.TestCase):
    def test_matching_malformed_domain_facts_are_not_silently_ignored(self):
        malformed_criteria = DomainFactCommitted(
            "helperme.criteria.committed.v1",
            {"version": 1},
        )
        malformed_judgment = DomainFactCommitted(
            "helperme.judgment.committed.v1",
            {"verdict": "done"},
        )

        with self.assertRaisesRegex(ValueError, "criteria fact"):
            criteria_from_fact(malformed_criteria)
        with self.assertRaisesRegex(ValueError, "judgment fact"):
            judgment_from_fact(malformed_judgment)

    def test_relax_inferred_does_not_change_user_objective(self):
        intent = classify_user_intent(
            CriteriaCommitted(
                version=1,
                user_objective="修这个 bug",
                strict_completion=True,
                inferred=inferred_from_facts(
                    SessionFacts(True, False, None),
                ),
                source=CriteriaSource.CLASSIFIER,
            ),
            "先改完，测试一会再说",
        )
        self.assertEqual(intent.kind, "relax_inferred")
        self.assertEqual(intent.deferred_ids, ("inf-verify",))

    def test_parse_judgment_reads_json_object(self):
        self.assertEqual(
            parse_judgment('好的\n{"verdict":"done","summary":"测试过了"}'),
            (JudgmentVerdict.DONE, "测试过了"),
        )
        self.assertIsNone(parse_judgment("还不行"))
