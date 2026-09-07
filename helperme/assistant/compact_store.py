"""Host-owned durable conversation routing and compact publication."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4


class CompactStore:
    def __init__(self, root: Path):
        self.path = root / "conversations.sqlite"
        existing = self.path.exists()
        with closing(self.connect()) as db, db:
            if existing:
                if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise ValueError("unsupported conversation store schema")
                for table, fields in {
                    "conversations": ["id", "current"],
                    "members": ["session", "conversation"],
                    "compactions": [
                        "source",
                        "reader",
                        "successor",
                        "upto",
                        "bundle",
                        "summary",
                        "prepared",
                        "failure",
                        "published",
                    ],
                    "deliveries": [
                        "conversation",
                        "source",
                        "id",
                        "target",
                        "content",
                        "accepted",
                    ],
                }.items():
                    columns = [
                        row[1] for row in db.execute(f"PRAGMA table_info({table})")
                    ]
                    if columns != fields:
                        raise ValueError(f"invalid conversation store table: {table}")
                return
            db.executescript("""
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, current TEXT NOT NULL UNIQUE);
                CREATE TABLE members (
                    session TEXT PRIMARY KEY, conversation TEXT NOT NULL);
                CREATE TABLE compactions (
                    source TEXT PRIMARY KEY, reader TEXT NOT NULL UNIQUE,
                    successor TEXT NOT NULL UNIQUE, upto INTEGER NOT NULL CHECK(upto > 0),
                    bundle TEXT NOT NULL, summary TEXT, prepared TEXT, failure TEXT, published INTEGER NOT NULL CHECK(published IN (0,1)));
                CREATE TABLE deliveries (
                    conversation TEXT NOT NULL, source TEXT NOT NULL, id TEXT NOT NULL,
                    target TEXT NOT NULL, content TEXT NOT NULL, accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
                    PRIMARY KEY(conversation, source, id));
                PRAGMA user_version=1;
            """)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def binding(self, session):
        with closing(self.connect()) as db:
            member = db.execute(
                "SELECT conversation FROM members WHERE session=?", (session,)
            ).fetchone()
            if member is None:
                return session, session
            row = db.execute(
                "SELECT id, current FROM conversations WHERE id=?",
                (member["conversation"],),
            ).fetchone()
            if row is None:
                raise ValueError("conversation binding is missing")
            return row["id"], row["current"]

    def register(self, session):
        with closing(self.connect()) as db, db:
            if db.execute(
                "SELECT 1 FROM members WHERE session=?", (session,)
            ).fetchone():
                return
            db.execute("INSERT INTO conversations VALUES (?, ?)", (session, session))
            db.execute("INSERT INTO members VALUES (?, ?)", (session, session))

    def job(self, source):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM compactions WHERE source=?", (source,)
            ).fetchone()
            return None if row is None else dict(row)

    def reader_job(self, reader):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM compactions WHERE reader=?", (reader,)
            ).fetchone()
            return None if row is None else dict(row)

    def start(self, source, upto, bundle):
        reader, successor = f"compact-{uuid4().hex}", f"session-{uuid4().hex}"
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO compactions VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 0)",
                (source, reader, successor, upto, json.dumps(bundle)),
            )
        return self.job(source)

    def fail(self, reader, failure):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE compactions SET failure=? WHERE reader=?",
                (json.dumps(failure), reader),
            )

    def prepare(self, source, fact):
        encoded = json.dumps(fact, ensure_ascii=False)
        with closing(self.connect()) as db, db:
            row = db.execute(
                "SELECT prepared FROM compactions WHERE source=?", (source,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown compact source")
            if row["prepared"] is not None and row["prepared"] != encoded:
                raise ValueError("conflicting prepared successor")
            db.execute(
                "UPDATE compactions SET prepared=? WHERE source=?", (encoded, source)
            )

    def finish(self, reader, summary):
        with closing(self.connect()) as db, db:
            row = db.execute(
                "SELECT summary FROM compactions WHERE reader=?", (reader,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown compactor")
            if row["summary"] is not None and row["summary"] != summary:
                raise ValueError("compactor returned conflicting handoffs")
            db.execute(
                "UPDATE compactions SET summary=? WHERE reader=?", (summary, reader)
            )

    def reserve_delivery(self, conversation, source, identity, target, content):
        with closing(self.connect()) as db, db:
            row = db.execute(
                "SELECT * FROM deliveries WHERE conversation=? AND source=? AND id=?",
                (conversation, source, identity),
            ).fetchone()
            if row is not None:
                if row["content"] != content:
                    raise ValueError("conflicting conversation delivery")
                return row["target"], bool(row["accepted"])
            db.execute(
                "INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, 0)",
                (conversation, source, identity, target, content),
            )
            return target, False

    def acknowledge(self, conversation, source, identity):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE deliveries SET accepted=1 WHERE conversation=? AND source=? AND id=?",
                (conversation, source, identity),
            )

    def pending_deliveries(self, conversation):
        with closing(self.connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM deliveries WHERE conversation=? AND accepted=0 ORDER BY rowid",
                    (conversation,),
                )
            ]

    def publish(self, source, successor):
        with closing(self.connect()) as db, db:
            row = db.execute(
                "SELECT conversation FROM members WHERE session=?", (source,)
            ).fetchone()
            conversation = row["conversation"]
            current = db.execute(
                "SELECT current FROM conversations WHERE id=?", (conversation,)
            ).fetchone()[0]
            if current == successor:
                return
            if current != source:
                raise ValueError("compact source is no longer current")
            if db.execute(
                "SELECT 1 FROM deliveries WHERE conversation=? AND accepted=0",
                (conversation,),
            ).fetchone():
                raise ValueError("cannot publish with unaccepted input")
            job = db.execute(
                "SELECT successor, summary, prepared FROM compactions WHERE source=?",
                (source,),
            ).fetchone()
            if (
                job["successor"] != successor
                or job["summary"] is None
                or job["prepared"] is None
            ):
                raise ValueError("successor has no handoff")
            db.execute("INSERT INTO members VALUES (?, ?)", (successor, conversation))
            db.execute(
                "UPDATE conversations SET current=? WHERE id=?",
                (successor, conversation),
            )
            db.execute("UPDATE compactions SET published=1 WHERE source=?", (source,))
