from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from helperme.assistant.runner import SessionNotFoundError
from helperme.runtime import RuntimeStatus, SqliteJournal, replay
from helperme.runtime.events import (
    DeliveryIdentity,
    DomainFactCommitted,
    EventDraft,
    UserMessageReceived,
)


@dataclass(frozen=True, slots=True)
class ForkedMessage:
    content: str
    artifact_refs: tuple[str, ...]


class ForkMessageNotFoundError(LookupError):
    pass


class SessionForkUnavailableError(ValueError):
    pass


class SessionStore:
    """Identity to directory mapping.

    Workers open existing Journals. Host may open one only when that Session
    has no Worker, to persist a parent-initiated return.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        if type(session_id) is not str or not session_id:
            raise ValueError("Session identity must be a nonempty string")
        key = sha256(session_id.encode("utf-8")).hexdigest()
        return self.root / key / "journal.sqlite"

    def require(self, session_id: str) -> Path:
        path = self.path(session_id)
        if not path.parent.exists():
            raise SessionNotFoundError(session_id)
        if not path.is_file():
            raise ValueError(f"Session Journal missing: {path}")
        return path

    def journals(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for entry in self.root.iterdir():
            if not entry.is_dir() or len(entry.name) != 64 or any(
                char not in "0123456789abcdef" for char in entry.name
            ):
                continue
            journal = entry / "journal.sqlite"
            if not journal.is_file():
                raise ValueError(f"Session Journal missing: {journal}")
            paths.append(journal)
        return tuple(sorted(paths))

    async def create(
        self, session_id: str, *, initial_fact: dict | None = None
    ) -> None:
        path = self.path(session_id)
        if path.parent.exists():
            raise ValueError(f"Session 已存在: {session_id}")
        staging = self.root / f".creating-{uuid4().hex}"
        staging.mkdir()
        journal = SqliteJournal(staging / "journal.sqlite")
        await journal.create_session(session_id)
        if initial_fact is not None:
            await journal.accept_delivery(
                EventDraft(
                    event_id=f"event_{uuid4().hex}",
                    session_id=session_id,
                    payload=DomainFactCommitted(
                        initial_fact["fact_type"],
                        initial_fact["data"],
                        requests_decision=initial_fact["requests_decision"],
                    ),
                    occurred_at=datetime.now(timezone.utc),
                    delivery=DeliveryIdentity(
                        initial_fact["source"], initial_fact["delivery_id"]
                    ),
                )
            )
        os.rename(staging, path.parent)

    async def fork_before_message(
        self,
        source_session_id: str,
        message_id: str,
        child_session_id: str,
    ) -> ForkedMessage:
        source_path = self.require(source_session_id)
        child_path = self.path(child_session_id)
        if child_path.parent.exists():
            raise ValueError(f"Session 已存在: {child_session_id}")

        source_events = await SqliteJournal(source_path).snapshot(source_session_id)
        target = next(
            (event for event in source_events if event.event_id == message_id),
            None,
        )
        if target is None:
            raise ForkMessageNotFoundError(message_id)
        if not isinstance(target.payload, UserMessageReceived):
            raise SessionForkUnavailableError(
                "fork target must be a user message"
            )
        prefix = tuple(
            event for event in source_events if event.sequence < target.sequence
        )
        state = replay(source_session_id, prefix).state
        if state.status is not RuntimeStatus.WAITING or state.waiting_for != (
            "user_message",
        ):
            raise SessionForkUnavailableError(
                "fork prefix must end at a user-message boundary"
            )

        staging = self.root / f".creating-{uuid4().hex}"
        staging.mkdir()
        try:
            await SqliteJournal(staging / "journal.sqlite").materialize_history(
                child_session_id,
                prefix,
            )
            for drawer in ("artifacts", ".attachments"):
                source_drawer = source_path.parent / drawer
                if source_drawer.is_dir():
                    shutil.copytree(source_drawer, staging / drawer)
            os.rename(staging, child_path.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return ForkedMessage(target.payload.content, target.artifact_refs)
