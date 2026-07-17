import unittest
from unittest.mock import Mock

from core.context import (
    ContextBudget,
    ContextManager,
    ContextPreparationService,
    ContextState,
    MicroCompactionConfig,
    MicroCompactionDecision,
    MicroCompactionPolicy,
    ModelBudgetConfig,
    SummaryGeneration,
    make_budget_assessment,
)
from core.messages import Conversation
from core.model_call import LLMResponse
from tests.core.llm_test_support import CharacterEstimator


class ContextPreparationServiceTest(unittest.TestCase):
    def test_builds_snapshot_from_policy_candidate_without_mutating_input_state(self):
        conversation = Conversation()
        conversation.set_system_prompt("system")
        conversation.add_user("old")
        conversation.add_user("recent")
        old_message = conversation.records[1]
        original_state = ContextState()
        candidate_state = ContextState(
            micro_compacted_through_message_id=old_message.message_id
        )
        assessment = make_budget_assessment(10, 100)
        policy = Mock()
        policy.propose.return_value = MicroCompactionDecision(
            candidate_state=candidate_state,
            before=assessment,
            after=assessment,
            changed=True,
        )
        service = ContextPreparationService(
            ContextManager(),
            policy,
            context_budget=Mock(),
            summary_generator=Mock(),
        )

        prepared = service.prepare(
            conversation_records=conversation.records,
            context_state=original_state,
            runtime_instructions=["runtime"],
            tools=[],
        )

        self.assertIs(prepared.context_state, candidate_state)
        self.assertEqual(original_state, ContextState())
        self.assertIn("runtime", prepared.model_context.messages[0]["content"])
        self.assertEqual(prepared.composition, assessment.composition)
        self.assertTrue(prepared.micro_compaction_trace.changed)
        self.assertEqual(
            prepared.micro_compaction_trace.boundary_message_id,
            old_message.message_id,
        )
        policy.propose.assert_called_once()

    def test_level2_summarizes_only_history_before_current_run(self):
        conversation = Conversation()
        conversation.set_system_prompt("system")
        conversation.add_user("old " * 30)
        conversation.add_assistant(
            LLMResponse(type="text", content="old answer " * 20)
        )
        boundary = conversation.records[-1]
        conversation.add_user("current goal")
        budget = ContextBudget(
            CharacterEstimator(),
            ModelBudgetConfig(context_limit=400, input_ratio=0.5),
        )
        manager = ContextManager()
        generator = RecordingSummaryGenerator("short handoff")
        service = ContextPreparationService(
            manager,
            MicroCompactionPolicy(
                manager,
                budget,
                MicroCompactionConfig(
                    trigger_ratio=0.5,
                    target_ratio=0.3,
                    recent_protection_tokens=10,
                ),
            ),
            context_budget=budget,
            summary_generator=generator,
        )

        prepared = service.prepare(
            conversation_records=conversation.records,
            context_state=ContextState(),
            runtime_instructions=[],
            tools=[],
            level2_boundary_message_id=boundary.message_id,
        )

        summary_source = generator.context.messages
        self.assertNotIn("current goal", str(summary_source))
        self.assertIn("old answer", str(summary_source))
        self.assertEqual(prepared.context_state.summary, "short handoff")
        self.assertEqual(
            prepared.context_state.summarized_through_message_id,
            boundary.message_id,
        )
        self.assertIsNone(
            prepared.context_state.micro_compacted_through_message_id
        )
        self.assertIsNotNone(prepared.summary_compaction)
        self.assertIsNone(prepared.blocked_assessment)
        self.assertEqual(
            prepared.composition,
            prepared.summary_compaction.after.composition,
        )

    def test_level2_rejects_summary_state_when_reassessment_is_still_over_budget(self):
        conversation = Conversation()
        conversation.set_system_prompt("system")
        conversation.add_user("old task")
        conversation.add_assistant(LLMResponse(type="text", content="old answer"))
        boundary = conversation.records[-1]
        conversation.add_user("current goal")
        before = make_budget_assessment(900, 750)
        after = make_budget_assessment(800, 750)
        policy = Mock()
        policy.propose.return_value = MicroCompactionDecision(
            candidate_state=ContextState(),
            before=before,
            after=before,
            changed=False,
        )
        budget = Mock()
        budget.assess.return_value = after
        service = ContextPreparationService(
            ContextManager(),
            policy,
            context_budget=budget,
            summary_generator=RecordingSummaryGenerator("handoff"),
        )

        prepared = service.prepare(
            conversation_records=conversation.records,
            context_state=ContextState(),
            runtime_instructions=[],
            tools=[],
            level2_boundary_message_id=boundary.message_id,
        )

        self.assertEqual(prepared.blocked_assessment, after)
        self.assertEqual(prepared.context_state, ContextState())
        self.assertEqual(prepared.composition, after.composition)


class RecordingSummaryGenerator:
    def __init__(self, summary):
        self.summary = summary
        self.context = None

    def generate(self, model_context):
        self.context = model_context
        return SummaryGeneration(
            summary=self.summary,
            input_tokens=100,
            output_tokens=10,
        )


if __name__ == "__main__":
    unittest.main()
