"""SQLite persistence layer for Claude Code session metrics."""

import sqlite3
import time
import uuid
from pathlib import Path

DB_DIR = Path.home() / ".claude" / "statusline"
DB_PATH = DB_DIR / "usage.db"
CONV_ID_FILE = DB_DIR / ".conv_id"


def _conv_id() -> str:
    """Get or create a conversation ID shared by main session + subagents."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cid = CONV_ID_FILE.read_text().strip()
        if cid:
            return cid
    except (OSError, FileNotFoundError):
        pass
    cid = uuid.uuid4().hex[:12]
    CONV_ID_FILE.write_text(cid)
    return cid

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    model_id TEXT,
    model_name TEXT,
    started_at INTEGER NOT NULL,
    last_updated INTEGER NOT NULL,
    turn_count INTEGER DEFAULT 0,
    tot_input_tokens INTEGER DEFAULT 0,
    tot_output_tokens INTEGER DEFAULT 0,
    tot_cache_read_tokens INTEGER DEFAULT 0,
    tot_cache_write_tokens INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    subagent_total INTEGER DEFAULT 0,
    subagent_running INTEGER DEFAULT 0,
    is_stale INTEGER DEFAULT 0,
    last_snapshot INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_number INTEGER,
    tool_name TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS subagent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_type TEXT,
    agent_id TEXT NOT NULL,
    event TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_subagent_events_session ON subagent_events(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_stale ON sessions(is_stale);
"""

_db_inited = False
_known_sessions: set[str] = set()


def _conn(path: Path | None = None) -> sqlite3.Connection:
    dbp = Path(path) if path else DB_PATH
    dbp.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(dbp))
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db(path: Path | None = None) -> None:
    global _db_inited
    if _db_inited:
        return
    c = _conn(path)
    try:
        c.executescript(SCHEMA_SQL)
        c.commit()
        _db_inited = True
    finally:
        c.close()


def ensure_session(sid: str, model_id: str = "", model_name: str = "") -> None:
    if sid in _known_sessions:
        return
    c = _conn()
    try:
        now = int(time.time())
        cid = _conv_id()
        c.execute("INSERT OR IGNORE INTO sessions (session_id, conversation_id, model_id, model_name, started_at, last_updated) VALUES (?,?,?,?,?,?)",
                  (sid, cid, model_id, model_name, now, now))
        c.execute("UPDATE sessions SET last_updated=?, model_id=?, model_name=? WHERE session_id=?",
                  (now, model_id, model_name, sid))
        c.commit()
        _known_sessions.add(sid)
    finally:
        c.close()


def accumulate(
    sid: str,
    total_input: int,
    total_output: int,
    per_call_input: int,
    per_call_output: int,
    per_call_cache_read: int,
    per_call_cache_write: int,
) -> None:
    c = _conn()
    try:
        now = int(time.time())
        row = c.execute("SELECT last_snapshot, tot_input_tokens, tot_output_tokens, tot_cache_read_tokens, tot_cache_write_tokens FROM sessions WHERE session_id=?",
                        (sid,)).fetchone()

        last_snap = row[0] if row else 0
        cum_in = (row[1] or 0) if row else 0
        cum_out = (row[2] or 0) if row else 0
        cum_cr = (row[3] or 0) if row else 0
        cum_cw = (row[4] or 0) if row else 0

        if total_input != last_snap:
            cum_in += per_call_input
            cum_out += per_call_output
            cum_cr += per_call_cache_read
            cum_cw += per_call_cache_write

        c.execute("""UPDATE sessions SET
            tot_input_tokens=?, tot_output_tokens=?, tot_cache_read_tokens=?, tot_cache_write_tokens=?,
            turn_count=CASE WHEN ? != ? THEN turn_count+1 ELSE turn_count END,
            last_snapshot=?, last_updated=? WHERE session_id=?""",
                  (cum_in, cum_out, cum_cr, cum_cw, total_input, last_snap, total_input, now, sid))
        c.commit()
    finally:
        c.close()


def record_tool_call(sid: str, tool_name: str) -> None:
    c = _conn()
    try:
        ts = int(time.time())
        c.execute("INSERT INTO tool_calls (session_id, tool_name, timestamp) VALUES (?,?,?)",
                  (sid, tool_name, ts))
        c.execute("UPDATE sessions SET tool_call_count=tool_call_count+1, last_updated=? WHERE session_id=?",
                  (ts, sid))
        c.commit()
    finally:
        c.close()


def record_subagent_start(sid: str, agent_type: str, agent_id: str) -> None:
    c = _conn()
    try:
        ts = int(time.time())
        c.execute("INSERT INTO subagent_events (session_id, agent_type, agent_id, event, timestamp) VALUES (?,?,?,'start',?)",
                  (sid, agent_type, agent_id, ts))
        c.execute("UPDATE sessions SET subagent_total=subagent_total+1, subagent_running=subagent_running+1, last_updated=? WHERE session_id=?",
                  (ts, sid))
        c.commit()
    finally:
        c.close()


def record_subagent_stop(sid: str, agent_type: str, agent_id: str) -> None:
    c = _conn()
    try:
        ts = int(time.time())
        c.execute("INSERT INTO subagent_events (session_id, agent_type, agent_id, event, timestamp) VALUES (?,?,?,'stop',?)",
                  (sid, agent_type, agent_id, ts))
        c.execute("UPDATE sessions SET subagent_running=MAX(subagent_running-1,0), last_updated=? WHERE session_id=?",
                  (ts, sid))
        c.commit()
    finally:
        c.close()


def get_session(sid: str) -> dict | None:
    c = _conn()
    try:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        return dict(row) if row else None
    finally:
        c.close()


def get_all_totals() -> dict:
    """Aggregate token totals for current conversation (main + subagents)."""
    c = _conn()
    try:
        cid = _conv_id()
        row = c.execute(
            "SELECT SUM(tot_input_tokens), SUM(tot_output_tokens), SUM(tot_cache_read_tokens), SUM(tot_cache_write_tokens), SUM(tool_call_count), SUM(subagent_total), SUM(subagent_running) FROM sessions WHERE conversation_id=? AND is_stale=0",
            (cid,)
        ).fetchone()
        return {
            "tot_input_tokens": row[0] or 0,
            "tot_output_tokens": row[1] or 0,
            "tot_cache_read_tokens": row[2] or 0,
            "tot_cache_write_tokens": row[3] or 0,
            "tool_call_count": row[4] or 0,
            "subagent_total": row[5] or 0,
            "subagent_running": row[6] or 0,
        }
    finally:
        c.close()


def cleanup_stale(max_age_days: int = 30) -> int:
    c = _conn()
    try:
        cutoff = int(time.time()) - max_age_days * 86400
        c.execute("UPDATE sessions SET is_stale=1 WHERE last_updated<?", (cutoff,))
        c.execute("DELETE FROM tool_calls WHERE session_id IN (SELECT session_id FROM sessions WHERE is_stale=1)")
        c.execute("DELETE FROM subagent_events WHERE session_id IN (SELECT session_id FROM sessions WHERE is_stale=1)")
        cur = c.execute("DELETE FROM sessions WHERE is_stale=1")
        c.commit()
        global _known_sessions
        _known_sessions.clear()
        return cur.rowcount
    finally:
        c.close()
