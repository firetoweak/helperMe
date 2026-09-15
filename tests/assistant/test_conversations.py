from __future__ import annotations

import unittest
from datetime import datetime, timezone

from helperme.assistant.conversations import (
    project_conversation,
    project_session_summary,
)
from helperme.assistant.sessions import SessionView
from helperme.runtime import (
    Command,
    CommandOutcome,
    CommandOutcomeReceived,
    Event,
    InvokeTool,
    ModelDecision,
    OutcomeStatus,
    Step,
    StepCommitted,
    UserMessageReceived,
)


def event(sequence, event_id, payload):
    return Event(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        payload=payload,
        occurred_at=datetime(2026, 9, 15, sequence, tzinfo=timezone.utc),
        causation_id=None,
        correlation_id=None,
        schema_version=5,
        artifact_refs=(),
    )


def committed_step(event_id, trigger, content, commands):
    return StepCommitted(
        Step(
            step_id=event_id,
            trigger_event_id=trigger,
            decision_cursor=1,
            basis_state_version="basis",
            observed_journal_position=1,
            decision=ModelDecision(content, tuple(command.effect for command in commands)),
            commands=commands,
        )
    )


class ConversationProjectionTest(unittest.TestCase):
    def test_projects_user_text_assistant_text_and_tool_cards(self):
        read = Command("cmd-read", InvokeTool("read_file"))
        deliver = Command("cmd-deliver", InvokeTool("deliver"))
        events = (
            event(1, "user-1", UserMessageReceived("你好")),
            event(
                2,
                "step-1",
                committed_step(
                    "decision-1",
                    "user-1",
                    "  世界  ",
                    (read, deliver),
                ),
            ),
            event(
                3,
                "out-1",
                CommandOutcomeReceived(
                    "cmd-read",
                    "att-1",
                    CommandOutcome(OutcomeStatus.SUCCEEDED, value="ok"),
                ),
            ),
        )
        view = SessionView("waiting", ("user_message",), (), False)

        conversation = project_conversation("session-1", events, session=view)

        self.assertEqual(conversation.revision, 3)
        self.assertEqual(
            [
                (
                    item.kind,
                    getattr(item, "message_id", None),
                    getattr(item, "output_id", None),
                    getattr(item, "command_id", None),
                    getattr(item, "name", None),
                    getattr(item, "status", None),
                    getattr(item, "text", None),
                )
                for item in conversation.items
            ],
            [
                ("user", "user-1", None, None, None, None, "你好"),
                ("assistant", "step-1", "user-1", None, None, None, "世界"),
                ("tool", None, None, "cmd-read", "read_file", "succeeded", None),
            ],
        )

    def test_tool_without_outcome_is_running_and_failed_outcome_keeps_error(self):
        search = Command("cmd-search", InvokeTool("web_search"))
        events = (
            event(
                1,
                "step-1",
                committed_step("decision-1", "user-1", "", (search,)),
            ),
        )
        view = SessionView("waiting", ("user_message",), (), False)

        running = project_conversation("session-1", events, session=view)
        self.assertEqual(running.items[0].kind, "tool")
        self.assertEqual(running.items[0].status, "running")
        self.assertIsNone(running.items[0].error)

        failed = project_conversation(
            "session-1",
            events
            + (
                event(
                    2,
                    "out-1",
                    CommandOutcomeReceived(
                        "cmd-search",
                        "att-1",
                        CommandOutcome(OutcomeStatus.FAILED, error_message="boom"),
                    ),
                ),
            ),
            session=view,
        )
        self.assertEqual(failed.items[0].status, "failed")
        self.assertEqual(failed.items[0].error, "boom")

    def test_summary_uses_first_user_message_and_last_event_time(self):
        events = (
            event(1, "user-1", UserMessageReceived("第一行\n第二行")),
            event(2, "user-2", UserMessageReceived("之后")),
        )

        summary = project_session_summary("session-1", events, activity="running")

        self.assertEqual(summary.title, "第一行")
        self.assertEqual(summary.updated_at, events[-1].occurred_at)
        self.assertEqual(summary.activity, "running")
