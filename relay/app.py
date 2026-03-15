"""Agent relay — lightweight chat bridge between Claude Code instances.

Run:  uvicorn app:app --host 0.0.0.0 --port 8484
Docs: GET /docs
"""

import os
import time
import sqlite3
import hashlib
import hmac
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("RELAY_DB", str(Path(__file__).parent / "relay.db"))
API_KEY = os.environ.get("RELAY_API_KEY", "")  # set in .env; empty = no auth
MAX_MESSAGES = 500  # keep last N messages per channel

app = FastAPI(title="Agent Relay", version="0.1.0")


# ── Database ────────────────────────────────────────────────────────────────

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL DEFAULT 'general',
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_channel_ts
            ON messages (channel, ts)
        """)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Auth ────────────────────────────────────────────────────────────────────

def check_auth(authorization: str | None):
    if not API_KEY:
        return  # no key configured = open (dev mode)
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, API_KEY):
        raise HTTPException(403, "Invalid API key")


# ── Models ──────────────────────────────────────────────────────────────────

class Message(BaseModel):
    role: str  # e.g. "local", "hpc", "arbutus"
    content: str
    channel: str = "general"


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    channel: str
    ts: float


# ── Routes ──────────────────────────────────────────────────────────────────

@app.post("/api/chat", response_model=MessageOut)
def post_message(msg: Message, authorization: str | None = Header(None)):
    """Post a message to a channel."""
    check_auth(authorization)
    ts = time.time()
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO messages (channel, role, content, ts) VALUES (?, ?, ?, ?)",
            (msg.channel, msg.role, msg.content, ts),
        )
        msg_id = cur.lastrowid

        # Prune old messages
        db.execute("""
            DELETE FROM messages WHERE channel = ? AND id NOT IN (
                SELECT id FROM messages WHERE channel = ? ORDER BY id DESC LIMIT ?
            )
        """, (msg.channel, msg.channel, MAX_MESSAGES))

    return MessageOut(id=msg_id, role=msg.role, content=msg.content,
                      channel=msg.channel, ts=ts)


@app.get("/api/chat", response_model=list[MessageOut])
def get_messages(
    channel: str = "general",
    since: float = 0,
    last: int = Query(default=50, le=200),
    authorization: str | None = Header(None),
):
    """Get messages from a channel. Use since=<timestamp> for polling."""
    check_auth(authorization)
    with get_db() as db:
        rows = db.execute(
            """SELECT id, role, content, channel, ts FROM messages
               WHERE channel = ? AND ts > ?
               ORDER BY id DESC LIMIT ?""",
            (channel, since, last),
        ).fetchall()
    return [MessageOut(**dict(r)) for r in reversed(rows)]


@app.get("/api/channels")
def list_channels(authorization: str | None = Header(None)):
    """List active channels with message counts."""
    check_auth(authorization)
    with get_db() as db:
        rows = db.execute(
            """SELECT channel, COUNT(*) as count, MAX(ts) as last_ts
               FROM messages GROUP BY channel ORDER BY last_ts DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/chat/{channel}")
def clear_channel(channel: str, authorization: str | None = Header(None)):
    """Clear all messages in a channel."""
    check_auth(authorization)
    with get_db() as db:
        db.execute("DELETE FROM messages WHERE channel = ?", (channel,))
    return {"cleared": channel}


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Startup ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
