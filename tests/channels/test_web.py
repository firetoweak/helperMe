from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient
from PIL import Image

from helperme.assistant.attachments import AttachmentGateway
from helperme.assistant.conversations import (
    ConversationView,
    SessionSummary,
    UserItem,
)
from helperme.assistant.runner import SessionNotFoundError
from helperme.assistant.sessions import SessionView
from helperme.channels.web.app import create_web_app
from helperme.channels.web.channel import WebChannel
from helperme.channels.web.hub import WebEventHub


class _Sessions:
    def __init__(self, queries):
        self.queries = queries
        self.calls = []

    async def create(self, session_id):
        self.calls.append(("create", session_id))

    async def select(self, owner, session_id):
        self.calls.append(("select", owner, session_id))
        if session_id == "session-missing":
            raise SessionNotFoundError(session_id)
        return SessionView("waiting", ("user_message",), (), False)

    async def accept_input(self, session_id, content, **kwargs):
        self.calls.append(("accept_input", session_id, content, kwargs))
        self.queries.record(session_id, content)
        return SessionView("waiting", ("user_message",), (), False)

    async def cancel_turn(self, session_id):
        self.calls.append(("cancel_turn", session_id))
        return SessionView("waiting", ("user_message",), (), False)

    async def fork_and_accept_input(
        self, owner, source_session_id, message_id, content, **kwargs
    ):
        self.calls.append(
            (
                "fork_and_accept_input",
                owner,
                source_session_id,
                message_id,
                content,
                kwargs,
            )
        )
        self.queries.record(kwargs["child_session_id"], content)
        return SessionView("runnable", (), (), True)

    async def release(self, owner):
        self.calls.append(("release", owner))


class _Queries:
    def __init__(self):
        self.accepted: list[tuple[str, str]] = []

    def record(self, session_id, content):
        self.accepted.append((session_id, content))

    async def list_sessions(self):
        return (SessionSummary("session-old", "旧会话", None, "idle"),)

    async def conversation(self, session_id, *, view=None):
        items = tuple(
            UserItem(
                "user",
                f"user-{index}",
                text,
                datetime(2026, 9, 15, tzinfo=timezone.utc),
            )
            for index, (item_session, text) in enumerate(self.accepted)
            if item_session == session_id
        )
        return ConversationView(
            session_id,
            len(items),
            items,
            view or SessionView("waiting", ("user_message",), (), False),
        )


class WebFirstSliceTest(unittest.TestCase):
    def setUp(self):
        self._directory = TemporaryDirectory()
        self.queries = _Queries()
        self.sessions = _Sessions(self.queries)
        self.channel = WebChannel(
            self.sessions,
            self.queries,
            AttachmentGateway(Path(self._directory.name)),
        )
        self.hub = WebEventHub()
        self.connection = self.channel.connect()
        self.client = TestClient(create_web_app(self.channel, self.hub))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self._directory.cleanup()

    def test_lists_sessions_and_creates_selected_conversation(self):
        listed = self.client.get("/api/sessions")
        created = self.client.post(
            "/api/sessions",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()[0]["title"], "旧会话")
        self.assertEqual(created.status_code, 201)
        session_id = created.json()["session_id"]
        self.assertEqual(self.sessions.calls[0], ("create", session_id))
        self.assertEqual(
            self.sessions.calls[1],
            ("select", self.connection.owner, session_id),
        )

    def test_events_assigns_connection_identity_and_releases_on_disconnect(self):
        body = bytearray()
        headers = {}

        async def exercise():
            first = asyncio.Event()

            async def receive():
                await first.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    headers.update(
                        (key.decode(), value.decode())
                        for key, value in message.get("headers", [])
                    )
                elif message["type"] == "http.response.body":
                    chunk = message.get("body") or b""
                    body.extend(chunk)
                    if chunk:
                        first.set()

            async with asyncio.timeout(2):
                await self.client.app(
                    {
                        "type": "http",
                        "asgi": {"version": "3.0", "spec_version": "2.3"},
                        "http_version": "1.1",
                        "method": "GET",
                        "scheme": "http",
                        "path": "/api/events",
                        "raw_path": b"/api/events",
                        "root_path": "",
                        "query_string": b"",
                        "headers": [],
                        "client": ("testclient", 50000),
                        "server": ("testserver", 80),
                    },
                    receive,
                    send,
                )

        self.client.portal.call(exercise)

        self.assertIn("text/event-stream", headers["content-type"])
        event = None
        payload = None
        for line in body.decode().splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1])
        self.assertEqual(event, "connected")
        connection_id = payload["connection_id"]
        self.assertNotIn(connection_id, self.channel._connections)
        self.assertIn(("release", f"web:{connection_id}"), self.sessions.calls)

    def test_reads_conversation_without_starting_a_worker(self):
        response = self.client.get("/api/sessions/session-old")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "session-old")
        self.assertEqual(response.json()["items"], [])
        self.assertEqual(self.sessions.calls, [])

    def test_runtime_exposes_active_model_and_context_limit(self):
        response = self.client.get("/api/runtime")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"model": "test", "context_limit": 200000},
        )

    def test_selecting_unknown_session_is_a_client_error(self):
        response = self.client.post(
            "/api/sessions/session-missing/select",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 404)

    def test_selecting_another_session_only_changes_owner_selection(self):
        response = self.client.post(
            "/api/sessions/session-old/select",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.sessions.calls,
            [("select", self.connection.owner, "session-old")],
        )

    def test_accept_input_returns_user_message_and_does_not_wait_for_model(self):
        response = self.client.post(
            "/api/sessions/session-old/inputs",
            json={
                "connection_id": self.connection.connection_id,
                "delivery_id": "delivery-1",
                "text": "  你好  ",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["text"], "你好")
        self.assertEqual(response.json()["items"][0]["kind"], "user")
        self.assertEqual(
            self.sessions.calls,
            [
                (
                    "accept_input",
                    "session-old",
                    "你好",
                    {"delivery_id": "delivery-1", "source": "web"},
                )
            ],
        )

    def test_cancel_only_targets_the_requested_session(self):
        response = self.client.post(
            "/api/sessions/session-old/cancel",
            json={"connection_id": self.connection.connection_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sessions.calls, [("cancel_turn", "session-old")])

    def test_editing_user_message_creates_and_selects_a_new_branch(self):
        response = self.client.post(
            "/api/sessions/session-old/forks",
            json={
                "connection_id": self.connection.connection_id,
                "message_id": "user-1",
                "delivery_id": "edit-1",
                "text": "  修改后  ",
            },
        )

        self.assertEqual(response.status_code, 201)
        child_session_id = response.json()["session_id"]
        self.assertNotEqual(child_session_id, "session-old")
        self.assertEqual(response.json()["items"][0]["text"], "修改后")
        self.assertEqual(
            self.sessions.calls,
            [
                (
                    "fork_and_accept_input",
                    self.connection.owner,
                    "session-old",
                    "user-1",
                    "修改后",
                    {
                        "child_session_id": child_session_id,
                        "delivery_id": "edit-1",
                        "source": "web",
                    },
                )
            ],
        )

    def test_upload_and_send_image_forwards_session_refs(self):
        uploaded = self.client.post(
            "/api/sessions/session-old/attachments",
            data={"connection_id": self.connection.connection_id},
            files={"file": ("shot.png", _png_bytes(), "image/png")},
        )

        self.assertEqual(uploaded.status_code, 201)
        attachment_id = uploaded.json()["attachment_id"]
        self.assertEqual(uploaded.json()["mime"], "image/png")
        downloaded = self.client.get(
            f"/api/sessions/session-old/attachments/{attachment_id}"
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.headers["content-type"], "image/png")
        self.assertEqual(downloaded.content, _png_bytes())

        sent = self.client.post(
            "/api/sessions/session-old/inputs",
            json={
                "connection_id": self.connection.connection_id,
                "delivery_id": "delivery-image",
                "text": "[Image #1]",
                "artifact_refs": [attachment_id],
            },
        )

        self.assertEqual(sent.status_code, 200)
        self.assertEqual(
            self.sessions.calls[-1],
            (
                "accept_input",
                "session-old",
                "[Image #1]",
                {
                    "delivery_id": "delivery-image",
                    "source": "web",
                    "artifact_refs": (attachment_id,),
                },
            ),
        )

    def test_rejected_upload_is_a_client_error(self):
        response = self.client.post(
            "/api/sessions/session-old/attachments",
            data={"connection_id": self.connection.connection_id},
            files={"file": ("notes.txt", b"not an image", "text/plain")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.json())


def _png_bytes():
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    return buffer.getvalue()
