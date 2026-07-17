import json
import unittest

from core.context import (
    ContextBudget,
    ContextComposition,
    ContextManager,
    ContextRequest,
    ContextState,
    MicroCompactionConfig,
    MicroCompactionPolicy,
    ModelBudgetConfig,
    ModelContext,
)
from core.context.composition import (
    ROLE_KEYS,
    empty_role_counts,
    empty_role_tokens,
)
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall


class CharacterEstimator:
    def estimate(self, model_context: ModelContext, tools: list[dict]) -> int:
        return len(
            json.dumps(
                {"messages": model_context.messages, "tools": tools},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def breakdown(
        self,
        model_context: ModelContext,
        tools: list[dict],
        *,
        input_budget_tokens: int,
    ) -> ContextComposition:
        total = self.estimate(model_context, tools)
        by_role = empty_role_tokens()
        counts = empty_role_counts()
        tool_result_chars = 0
        for message in model_context.messages:
            role = message.get("role")
            if role not in ROLE_KEYS:
                role = "assistant"
            size = len(json.dumps(message, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            by_role[role] += size
            counts[role] += 1
            if role == "tool":
                content = message.get("content", "")
                tool_result_chars += len(content) if isinstance(content, str) else 0
        tools_schema = len(
            json.dumps(tools, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        ) if tools else 0
        parts = sum(by_role.values()) + tools_schema
        if parts != total:
            by_role["assistant"] += total - parts
        return ContextComposition(
            estimated_total_tokens=total,
            input_budget_tokens=input_budget_tokens,
            tools_schema_tokens=tools_schema,
            by_role_tokens=by_role,
            by_role_message_counts=counts,
            tool_result_chars=tool_result_chars,
        )

    def calibrate(self, model_context, tools, actual_input_tokens):
        return None


def add_successful_batch(
    conversation: Conversation,
    call_id: str,
    result_size: int,
) -> None:
    conversation.add_assistant(
        LLMResponse(
            type="tool_calls",
            calls=[
                ToolCall(
                    call_id,
                    "read_file",
                    f'{{"path":"{call_id}.txt"}}',
                )
            ],
        )
    )
    conversation.add_tools_result(
        [
            {
                "tool_call_id": call_id,
                "content": json.dumps(
                    {
                        "ok": True,
                        "code": "OK",
                        "data": {"content": "x" * result_size},
                        "error": None,
                        "hint": None,
                    }
                ),
            }
        ]
    )
    conversation.add_assistant(
        LLMResponse(type="text", content=f"{call_id} consumed")
    )


class MicroCompactionConfigTest(unittest.TestCase):
    def test_rejects_invalid_watermarks_or_recent_window(self):
        invalid = (
            (0, 0.5, 100),
            (0.8, 0, 100),
            (0.8, 0.8, 100),
            (1.1, 0.5, 100),
            (0.8, 0.5, 0),
        )

        for trigger_ratio, target_ratio, recent_tokens in invalid:
            with self.subTest(
                trigger_ratio=trigger_ratio,
                target_ratio=target_ratio,
                recent_tokens=recent_tokens,
            ):
                with self.assertRaises(ValueError):
                    MicroCompactionConfig(
                        trigger_ratio=trigger_ratio,
                        target_ratio=target_ratio,
                        recent_protection_tokens=recent_tokens,
                    )


class MicroCompactionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.manager = ContextManager()
        self.budget = ContextBudget(
            estimator=CharacterEstimator(),
            config=ModelBudgetConfig(
                context_limit=2_000,
                input_ratio=0.75,
            ),
        )

    def _policy(self, recent_tokens=400):
        return MicroCompactionPolicy(
            context_manager=self.manager,
            context_budget=self.budget,
            config=MicroCompactionConfig(
                trigger_ratio=0.8,
                target_ratio=0.5,
                recent_protection_tokens=recent_tokens,
            ),
        )

    def test_below_high_watermark_returns_the_existing_state(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("hello")
        state = ContextState()

        decision = self._policy().propose(
            conversation_records=conversation.records,
            context_state=state,
            runtime_instructions=[],
            tools=[],
        )

        self.assertIs(decision.candidate_state, state)
        self.assertEqual(decision.before, decision.after)
        self.assertFalse(decision.changed)

    def test_advances_micro_boundary_without_touching_recent_tool_batch(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("old work")
        add_successful_batch(conversation, "call-old", result_size=900)
        conversation.add_user("recent work")
        add_successful_batch(conversation, "call-recent", result_size=700)
        state = ContextState()

        decision = self._policy(recent_tokens=900).propose(
            conversation_records=conversation.records,
            context_state=state,
            runtime_instructions=[],
            tools=[],
        )
        projected = self.manager.build(
            ContextRequest(
                conversation_records=conversation.records,
                runtime_instructions=[],
                context_state=decision.candidate_state,
            )
        )

        remaining_calls = [
            message
            for message in projected.messages
            if message.get("tool_calls")
        ]
        self.assertTrue(decision.changed)
        self.assertLess(
            decision.after.estimated_input_tokens,
            decision.before.estimated_input_tokens,
        )
        self.assertEqual(len(remaining_calls), 1)
        self.assertEqual(
            remaining_calls[0]["tool_calls"][0]["id"],
            "call-recent",
        )

    def test_existing_micro_boundary_never_moves_backward(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("old work")
        add_successful_batch(conversation, "call-old", result_size=900)
        existing_boundary = conversation.records[-1].message_id
        state = ContextState(
            micro_compacted_through_message_id=existing_boundary
        )
        conversation.add_user("recent work")
        add_successful_batch(conversation, "call-recent", result_size=700)

        decision = self._policy(recent_tokens=900).propose(
            conversation_records=conversation.records,
            context_state=state,
            runtime_instructions=[],
            tools=[],
        )

        ids = [record.message_id for record in conversation.records]
        self.assertGreaterEqual(
            ids.index(
                decision.candidate_state.micro_compacted_through_message_id
            ),
            ids.index(existing_boundary),
        )


if __name__ == "__main__":
    unittest.main()
