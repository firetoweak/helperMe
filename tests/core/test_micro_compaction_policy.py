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
    parse_tool_result_meta,
)
from core.messages import Conversation
from core.model_call import LLMResponse, ToolCall
from tests.core.llm_test_support import MemoryArtifactStore


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
            size = len(
                json.dumps(
                    message,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            by_role[role] += size
            counts[role] += 1
            if role == "tool":
                content = message.get("content", "")
                tool_result_chars += (
                    len(content) if isinstance(content, str) else 0
                )
        tools_schema = (
            len(
                json.dumps(
                    tools,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            if tools
            else 0
        )
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
) -> str:
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
    tool_message_id = conversation.records[-1].message_id
    conversation.add_assistant(
        LLMResponse(type="text", content=f"{call_id} consumed")
    )
    return tool_message_id


class MicroCompactionConfigTest(unittest.TestCase):
    def test_rejects_non_positive_recent_window(self):
        for recent_tokens in (0, -1):
            with self.subTest(recent_tokens=recent_tokens):
                with self.assertRaises(ValueError):
                    MicroCompactionConfig(
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
        self.store = MemoryArtifactStore()

    def _policy(self, recent_tokens=400):
        return MicroCompactionPolicy(
            context_manager=self.manager,
            context_budget=self.budget,
            config=MicroCompactionConfig(
                recent_protection_tokens=recent_tokens,
            ),
            artifact_store=self.store,
        )

    def test_without_eligible_history_keeps_state_and_skips_save(self):
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

        self.assertEqual(decision.candidate_state, state)
        self.assertFalse(decision.changed)
        self.assertEqual(self.store.contents, {})

    def test_dehydrates_outside_recent_window_and_saves_artifact(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("old work")
        old_tool_id = add_successful_batch(
            conversation, "call-old", result_size=900
        )
        conversation.add_user("recent work")
        add_successful_batch(conversation, "call-recent", result_size=700)

        decision = self._policy(recent_tokens=900).propose(
            conversation_records=conversation.records,
            context_state=ContextState(),
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

        self.assertTrue(decision.changed)
        self.assertIn(old_tool_id, decision.candidate_state.tool_artifacts)
        self.assertEqual(len(self.store.contents), 1)
        artifact_id = decision.candidate_state.tool_artifacts[old_tool_id]
        self.assertIn(artifact_id, self.store.contents)

        tool_messages = [
            message
            for message in projected.messages
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 2)
        old_projected = next(
            message
            for message in projected.messages
            if message.get("role") == "tool"
            and parse_tool_result_meta(message["content"])[1] == artifact_id
        )
        recent_projected = [
            message
            for message in tool_messages
            if message is not old_projected
        ][0]
        self.assertTrue(parse_tool_result_meta(old_projected["content"])[0])
        self.assertFalse(parse_tool_result_meta(recent_projected["content"])[0])
        self.assertIn("tool_calls", projected.messages[2])

    def test_second_propose_is_idempotent_without_extra_saves(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("old work")
        add_successful_batch(conversation, "call-old", result_size=900)
        conversation.add_user("recent work")
        add_successful_batch(conversation, "call-recent", result_size=700)

        first = self._policy(recent_tokens=900).propose(
            conversation_records=conversation.records,
            context_state=ContextState(),
            runtime_instructions=[],
            tools=[],
        )
        saved = dict(self.store.contents)

        second = self._policy(recent_tokens=900).propose(
            conversation_records=conversation.records,
            context_state=first.candidate_state,
            runtime_instructions=[],
            tools=[],
        )

        self.assertFalse(second.changed)
        self.assertEqual(second.candidate_state.tool_artifacts, first.candidate_state.tool_artifacts)
        self.assertEqual(self.store.contents, saved)

    def test_reuses_existing_externalized_artifact_id_without_save(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("old work")
        existing_id = "art_" + "c" * 32
        conversation.add_assistant(
            LLMResponse(
                type="tool_calls",
                calls=[ToolCall("call-ext", "read_file", "{}")],
            )
        )
        conversation.add_tools_result(
            [
                {
                    "tool_call_id": "call-ext",
                    "content": json.dumps(
                        {
                            "ok": True,
                            "code": "OK",
                            "data": {
                                "externalized": True,
                                "artifact_id": existing_id,
                                "size_chars": 20_000,
                                "preview": "head",
                            },
                            "error": None,
                            "hint": "read_artifact",
                        }
                    ),
                }
            ]
        )
        tool_id = conversation.records[-1].message_id
        conversation.add_assistant(
            LLMResponse(type="text", content="consumed")
        )
        conversation.add_user("recent " + ("y" * 800))

        decision = self._policy(recent_tokens=200).propose(
            conversation_records=conversation.records,
            context_state=ContextState(),
            runtime_instructions=[],
            tools=[],
        )

        self.assertTrue(decision.changed)
        self.assertEqual(
            decision.candidate_state.tool_artifacts[tool_id],
            existing_id,
        )
        self.assertEqual(self.store.contents, {})

    def test_tool_window_splits_recent_and_compressible_tool_mass(self):
        conversation = Conversation()
        conversation.set_system_prompt("system prompt")
        conversation.add_user("old work")
        add_successful_batch(conversation, "call-old", result_size=900)
        conversation.add_user("recent work")
        add_successful_batch(conversation, "call-recent", result_size=700)

        decision = self._policy(recent_tokens=900).propose(
            conversation_records=conversation.records,
            context_state=ContextState(),
            runtime_instructions=[],
            tools=[],
        )

        self.assertGreater(decision.tool_window.compressible_tool_chars, 0)
        self.assertGreater(decision.tool_window.recent_tool_chars, 0)
        self.assertIsNotNone(decision.tool_window.recent_start_message_id)


if __name__ == "__main__":
    unittest.main()
