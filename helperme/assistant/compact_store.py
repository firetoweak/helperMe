"""Durable background jobs; active context is owned by the business Journal."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class ConversationStatus:
    conversation_id: str
    session_id: str
    compact_count: int
    compact_phase: str | None


class CompactStore:
    def __init__(self, root):
        self.path = root / "conversations.sqlite"
        existing = self.path.exists()
        with closing(self.connect()) as db, db:
            if not existing:
                db.executescript("""
                CREATE TABLE compactions (
                    reader TEXT PRIMARY KEY, source TEXT NOT NULL, window TEXT,
                    upto INTEGER NOT NULL, bundle TEXT NOT NULL, deadline REAL NOT NULL,
                    max_calls INTEGER NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
                    summary TEXT, prepared TEXT, failure TEXT, published INTEGER NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX active_compaction ON compactions(source) WHERE published=0;
                """)

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def status(self, session):
        with closing(self.connect()) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM compactions WHERE source=? AND published=1",
                (session,),
            ).fetchone()[0]
        job = self.job(session)
        phase = (
            None
            if job is None
            else (
                "failed" if job["failure"] else "ready" if job["summary"] else "running"
            )
        )
        return ConversationStatus(session, session, count, phase)

    def job(self, source):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM compactions WHERE source=? AND published=0", (source,)
            ).fetchone()
            return None if row is None else dict(row)

    def reader_job(self, reader):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM compactions WHERE reader=?", (reader,)
            ).fetchone()
            return None if row is None else dict(row)

    def start(self, source, snapshot):
        reader = "compact-" + uuid4().hex
        bundle = {k: snapshot[k] for k in ("inherited", "bundle")}
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO compactions(reader,source,window,upto,bundle,deadline,max_calls) VALUES (?,?,?,?,?,?,?)",
                (
                    reader,
                    source,
                    snapshot["window"],
                    snapshot["position"],
                    json.dumps(bundle),
                    time.time() + snapshot["timeout"],
                    snapshot["max_calls"],
                ),
            )
        return self.reader_job(reader)

    def attempt(self, reader):
        with closing(self.connect()) as db, db:
            row = db.execute(
                "SELECT * FROM compactions WHERE reader=?", (reader,)
            ).fetchone()
            if row["failure"] or row["summary"] is not None:
                raise ValueError("handoff job is not running")
            if time.time() >= row["deadline"] or row["calls"] >= row["max_calls"]:
                return {"calls": row["calls"], "exhausted": True}
            db.execute("UPDATE compactions SET calls=calls+1 WHERE reader=?", (reader,))
            return {"calls": row["calls"] + 1, "exhausted": False}

    def fail(self, reader, failure):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE compactions SET failure=? WHERE reader=? AND summary IS NULL",
                (json.dumps(failure), reader),
            )

    def fail_publication(self, reader, failure):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE compactions SET failure=? WHERE reader=?",
                (json.dumps(failure), reader),
            )

    def prepare(self, reader, value):
        encoded = json.dumps(value, ensure_ascii=False)
        with closing(self.connect()) as db, db:
            row = db.execute(
                "SELECT prepared FROM compactions WHERE reader=?", (reader,)
            ).fetchone()
            if row["prepared"] is not None and row["prepared"] != encoded:
                raise ValueError("conflicting window publication")
            db.execute(
                "UPDATE compactions SET prepared=? WHERE reader=?", (encoded, reader)
            )

    def finish(self, reader, summary):
        with closing(self.connect()) as db, db:
            row = db.execute(
                "SELECT summary,failure FROM compactions WHERE reader=?", (reader,)
            ).fetchone()
            if row["failure"] is not None:
                raise ValueError("failed handoff cannot complete")
            if row["summary"] is not None and row["summary"] != summary:
                raise ValueError("conflicting handoff")
            db.execute(
                "UPDATE compactions SET summary=? WHERE reader=?", (summary, reader)
            )

    def publish(self, reader):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE compactions SET published=1 WHERE reader=? AND prepared IS NOT NULL",
                (reader,),
            )
