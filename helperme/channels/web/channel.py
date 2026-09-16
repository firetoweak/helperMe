from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class WebConnection:
    connection_id: str

    @property
    def owner(self) -> str:
        return f"web:{self.connection_id}"


class WebChannel:
    def __init__(self, sessions, queries) -> None:
        self._sessions = sessions
        self._queries = queries
        self._connections: set[str] = set()

    def connect(self) -> WebConnection:
        connection = WebConnection(f"connection-{uuid4().hex}")
        self._connections.add(connection.connection_id)
        return connection

    async def disconnect(self, connection: WebConnection) -> None:
        self._connections.remove(connection.connection_id)
        await self._sessions.release(connection.owner)

    async def list_sessions(self):
        return await self._queries.list_sessions()

    async def conversation(self, session_id: str):
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        return await self._queries.conversation(session_id)

    async def create(self, connection_id: str):
        connection = self._require_connection(connection_id)
        session_id = f"session-{uuid4().hex}"
        await self._sessions.create(session_id)
        view = await self._sessions.select(connection.owner, session_id)
        return await self._queries.conversation(session_id, view=view)

    async def select(self, connection_id: str, session_id: str):
        connection = self._require_connection(connection_id)
        view = await self._sessions.select(connection.owner, session_id)
        return await self._queries.conversation(session_id, view=view)

    async def accept_input(
        self,
        connection_id: str,
        session_id: str,
        text: str,
        delivery_id: str,
    ):
        self._require_connection(connection_id)
        content = self._require_text(text)
        if type(delivery_id) is not str or not delivery_id:
            raise ValueError("delivery_id must be a non-empty str")
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        view = await self._sessions.accept_input(
            session_id,
            content,
            delivery_id=delivery_id,
            source="web",
        )
        return await self._queries.conversation(session_id, view=view)

    async def edit_and_fork(
        self,
        connection_id: str,
        source_session_id: str,
        message_id: str,
        text: str,
        delivery_id: str,
    ):
        connection = self._require_connection(connection_id)
        content = self._require_text(text)
        for label, value in (
            ("source_session_id", source_session_id),
            ("message_id", message_id),
            ("delivery_id", delivery_id),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{label} must be a non-empty str")
        child_session_id = f"session-{uuid4().hex}"
        view = await self._sessions.fork_and_accept_input(
            connection.owner,
            source_session_id,
            message_id,
            content,
            child_session_id=child_session_id,
            delivery_id=delivery_id,
            source="web",
        )
        return await self._queries.conversation(child_session_id, view=view)

    async def cancel(self, connection_id: str, session_id: str):
        self._require_connection(connection_id)
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        view = await self._sessions.cancel_turn(session_id)
        return await self._queries.conversation(session_id, view=view)

    def _require_connection(self, connection_id: str) -> WebConnection:
        if type(connection_id) is not str or not connection_id:
            raise ValueError("connection_id must be a non-empty str")
        if connection_id not in self._connections:
            raise ValueError("Web connection is not active")
        return WebConnection(connection_id)

    @staticmethod
    def _require_text(text: str) -> str:
        if type(text) is not str:
            raise TypeError("text must be str")
        content = text.strip()
        if not content:
            raise ValueError("text must be a non-empty str")
        return content
