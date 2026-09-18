import hashlib
import json
import sqlite3
from pathlib import Path

p = Path.home() / ".helperme" / "runtime_sessions" / "conversations.sqlite"
db = sqlite3.connect(p)
db.row_factory = sqlite3.Row
row = dict(
    db.execute(
        "SELECT * FROM compactions WHERE source=?",
        ("session-f3ebd1f2573c40c6852e7e022604ec27",),
    ).fetchone()
)
fail = json.loads(row["failure"])
print("exception:", fail["exception_type"])
print("message:", fail["message"])
print("--- traceback ---")
print(fail["traceback"])
print("--- ids ---")
for sid in [
    "session-f3ebd1f2573c40c6852e7e022604ec27",
    "compact-a24884e98356434990d59d463fb13f1b",
    "session-6d529bf071444c37a453223164107aba",
    "compact-747a8781ec764f3cac0ee72cc9d93166",
]:
    print(sid, hashlib.sha256(sid.encode()).hexdigest())

att = "6d152d3c8bcfd80207c87e1142c55c80b6ab71ba0899355f5968ceb937efac9e"
root = Path.home() / ".helperme" / "runtime_sessions"
hits = list(root.rglob(att))
print("--- attachment search ---", len(hits))
for hit in hits[:20]:
    print(hit)
