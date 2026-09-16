from __future__ import annotations

import unittest
from datetime import datetime, timezone

from helperme.assistant.conversations import (
    UNKNOWN_TOOL_ERROR,
    project_conversation,
    project_session_summary,
)
from helperme.assistant.sessions import SessionView
from helperme.runtime import (
    Command,
    CommandOutcome,
    CommandOutcomeReceived,
    DispatchAttemptStarted,
    Event,
    InvokeTool,
    ModelDecision,
    OutcomeStatus,
    Step,
    StepCommitted,
    UserMessageReceived,
)


def event(sequence, event_id, payload, causation_id=None, artifact_refs=()):
    return Event(
        event_id=event_id,
        session_id="session-1",
        sequence=sequence,
        payload=payload,
        occurred_at=datetime(2026, 9, 15, sequence, tzinfo=timezone.utc),
        causation_id=causation_id,
        correlation_id=None,
        schema_version=5,
        artifact_refs=artifact_refs,
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
    def test_projects_each_decision_as_one_step_with_its_tools(self):
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
        self.assertEqual(conversation.items[0].kind, "user")
        step = conversation.items[1]
        self.assertEqual(step.kind, "step")
        self.assertEqual(step.step_id, "decision-1")
        self.assertEqual(step.output_id, "user-1")
        self.assertEqual(step.text, "世界")
        self.assertEqual(len(step.tools), 1)
        self.assertEqual(step.tools[0].command_id, "cmd-read")
        self.assertEqual(step.tools[0].name, "read_file")
        self.assertEqual(step.tools[0].status, "succeeded")

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
        self.assertEqual(running.items[0].kind, "step")
        self.assertEqual(running.items[0].tools[0].status, "running")
        self.assertIsNone(running.items[0].tools[0].error)

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
        self.assertEqual(failed.items[0].tools[0].status, "failed")
        self.assertEqual(failed.items[0].tools[0].error, "boom")

    def test_idle_unknown_attempt_is_interrupted_not_running(self):
        search = Command("cmd-search", InvokeTool("web_search"))
        events = (
            event(
                1,
                "step-1",
                committed_step("decision-1", "user-1", "", (search,)),
            ),
            event(
                2,
                "attempt-1",
                DispatchAttemptStarted("att-1", "cmd-search"),
                causation_id="step-1",
            ),
        )
        view = SessionView("waiting", ("command:cmd-search",), (), False)

        idle = project_conversation("session-1", events, session=view)
        self.assertEqual(idle.items[0].tools[0].status, "unknown")
        self.assertEqual(idle.items[0].tools[0].error, UNKNOWN_TOOL_ERROR)

        busy = project_conversation(
            "session-1",
            events,
            session=view,
            activity="running",
        )
        self.assertEqual(busy.items[0].tools[0].status, "running")
        self.assertIsNone(busy.items[0].tools[0].error)

    def test_summary_uses_first_user_message_and_last_event_time(self):
        events = (
            event(1, "user-1", UserMessageReceived("第一行\n第二行")),
            event(2, "user-2", UserMessageReceived("之后")),
        )

        summary = project_session_summary("session-1", events, activity="running")

        self.assertEqual(summary.title, "第一行")
        self.assertEqual(summary.updated_at, events[-1].occurred_at)
        self.assertEqual(summary.activity, "running")

    def test_user_message_carries_image_refs(self):
        attachment_id = "sha256:" + "a" * 64
        conversation = project_conversation(
            "session-1",
            (
                event(
                    1,
                    "user-1",
                    UserMessageReceived("[Image #1]"),
                    artifact_refs=(attachment_id,),
                ),
            ),
            session=SessionView("waiting", ("user_message",), (), False),
        )

        self.assertEqual(conversation.items[0].kind, "user")
        self.assertEqual(conversation.items[0].text, "[Image #1]")
        self.assertEqual(conversation.items[0].images, (attachment_id,))


class ListSessionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_omits_journals_without_user_messages(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from helperme.assistant.conversations import AssistantQueries
        from helperme.assistant.host.session_store import SessionStore
        from helperme.runtime import SqliteJournal
        from helperme.runtime.events import DeliveryIdentity, EventDraft

        class Idle:
            def activity(self, session_id):
                return "idle"

        with TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            queries = AssistantQueries(store, Idle())
            await store.create("empty")
            await store.create("spoken")
            await SqliteJournal(store.require("spoken")).accept_delivery(
                EventDraft(
                    event_id="user-1",
                    session_id="spoken",
                    payload=UserMessageReceived("你好"),
                    occurred_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
                    delivery=DeliveryIdentity("web", "d1"),
                )
            )
            listed = await queries.list_sessions()

        self.assertEqual([item.session_id for item in listed], ["spoken"])
        self.assertEqual(listed[0].title, "你好")
