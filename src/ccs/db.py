"""SQLite persistence layer for Claude Code session metrics.

会话聚合策略：每个 Claude Code 对话由一个唯一的 session_id 标识。
官方文档确认子代理与主会话共享同一个 session_id，仅通过额外的 agent_id
字段区分。因此所有 hook 事件天然归入同一会话，无需子代理链接逻辑。

conversation_id 列保留用于未来扩展，当前始终等于 session_id。
"""

import sqlite3
import time
from pathlib import Path

DB_DIR = Path.home() / ".claude" / "statusline"
DB_PATH = DB_DIR / "usage.db"

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

CREATE TABLE IF NOT EXISTS model_usage (
    session_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    tot_input_tokens INTEGER DEFAULT 0,
    tot_output_tokens INTEGER DEFAULT 0,
    tot_cache_read_tokens INTEGER DEFAULT 0,
    tot_cache_write_tokens INTEGER DEFAULT 0,
    last_snapshot INTEGER DEFAULT 0,
    last_updated INTEGER NOT NULL,
    PRIMARY KEY (session_id, model_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_subagent_events_session ON subagent_events(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_stale ON sessions(is_stale);
CREATE INDEX IF NOT EXISTS idx_model_usage_session ON model_usage(session_id);
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
        c.execute(
            "INSERT OR IGNORE INTO sessions (session_id, conversation_id, model_id, model_name, started_at, last_updated) VALUES (?,?,?,?,?,?)",
            (sid, sid, model_id, model_name, now, now),
        )
        c.execute(
            "UPDATE sessions SET last_updated=?, model_id=?, model_name=?, is_stale=0 WHERE session_id=?",
            (now, model_id, model_name, sid),
        )
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
    model_id: str = "",
) -> None:
    """累加 token 到会话和按模型两个层级。

    会话层级（sessions 表）：聚合显示用，保持向后兼容。
    模型层级（model_usage 表）：每模型独立 snapshot 检测，用于精确成本计算。
    """
    c = _conn()
    try:
        now = int(time.time())

        # --- 会话层级累加 ---
        row = c.execute(
            "SELECT last_snapshot, tot_input_tokens, tot_output_tokens, tot_cache_read_tokens, tot_cache_write_tokens FROM sessions WHERE session_id=?",
            (sid,),
        ).fetchone()

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

        # --- 模型层级累加（独立 snapshot） ---
        if model_id:
            mrow = c.execute(
                "SELECT last_snapshot, tot_input_tokens, tot_output_tokens, tot_cache_read_tokens, tot_cache_write_tokens FROM model_usage WHERE session_id=? AND model_id=?",
                (sid, model_id),
            ).fetchone()

            m_last = mrow[0] if mrow else 0
            m_in = (mrow[1] or 0) if mrow else 0
            m_out = (mrow[2] or 0) if mrow else 0
            m_cr = (mrow[3] or 0) if mrow else 0
            m_cw = (mrow[4] or 0) if mrow else 0

            if total_input != m_last:
                m_in += per_call_input
                m_out += per_call_output
                m_cr += per_call_cache_read
                m_cw += per_call_cache_write

            c.execute("""INSERT OR REPLACE INTO model_usage
                (session_id, model_id, tot_input_tokens, tot_output_tokens, tot_cache_read_tokens, tot_cache_write_tokens, last_snapshot, last_updated)
                VALUES (?,?,?,?,?,?,?,?)""",
                      (sid, model_id, m_in, m_out, m_cr, m_cw, total_input, now))

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


def get_all_totals(sid: str = "") -> dict:
    """返回 *sid* 所属会话的聚合统计。

    通过 conversation_id 隔离不同对话，并发 Claude Code 实例互不干扰。
    """
    c = _conn()
    try:
        if sid:
            row = c.execute(
                "SELECT conversation_id FROM sessions WHERE session_id=?", (sid,)
            ).fetchone()
            cid = row[0] if row else sid
        else:
            row = c.execute(
                "SELECT conversation_id FROM sessions WHERE is_stale=0 ORDER BY last_updated DESC LIMIT 1"
            ).fetchone()
            cid = row[0] if row else ""

        if not cid:
            return {
                "tot_input_tokens": 0, "tot_output_tokens": 0,
                "tot_cache_read_tokens": 0, "tot_cache_write_tokens": 0,
                "tool_call_count": 0, "subagent_total": 0, "subagent_running": 0,
            }

        row = c.execute(
            "SELECT SUM(tot_input_tokens), SUM(tot_output_tokens), SUM(tot_cache_read_tokens), SUM(tot_cache_write_tokens), SUM(tool_call_count), SUM(subagent_total), SUM(subagent_running) FROM sessions WHERE conversation_id=? AND is_stale=0",
            (cid,),
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


def get_model_breakdown(sid: str) -> dict:
    """返回 *sid* 所属对话按模型分解的 token 用量。

    返回值: {model_id: {input, output, cache_read, cache_write}, ...}
    """
    c = _conn()
    try:
        row = c.execute(
            "SELECT conversation_id FROM sessions WHERE session_id=?", (sid,)
        ).fetchone()
        cid = row[0] if row else sid

        rows = c.execute(
            "SELECT mu.model_id, mu.tot_input_tokens, mu.tot_output_tokens, mu.tot_cache_read_tokens, mu.tot_cache_write_tokens FROM model_usage mu INNER JOIN sessions s ON mu.session_id = s.session_id WHERE s.conversation_id=? AND s.is_stale=0",
            (cid,),
        ).fetchall()

        result = {}
        for r in rows:
            result[r[0]] = {
                "input": r[1] or 0,
                "output": r[2] or 0,
                "cache_read": r[3] or 0,
                "cache_write": r[4] or 0,
            }
        return result
    finally:
        c.close()


def _cleanup_stale_rows(c: sqlite3.Connection) -> None:
    c.execute("DELETE FROM model_usage WHERE session_id IN (SELECT session_id FROM sessions WHERE is_stale=1)")
    c.execute("DELETE FROM tool_calls WHERE session_id IN (SELECT session_id FROM sessions WHERE is_stale=1)")
    c.execute("DELETE FROM subagent_events WHERE session_id IN (SELECT session_id FROM sessions WHERE is_stale=1)")
    c.execute("DELETE FROM sessions WHERE is_stale=1")
    global _known_sessions
    _known_sessions.clear()


def cleanup_stale(max_age_days: int = 30) -> int:
    """清理超过 *max_age_days* 天未更新的会话。可在 init_db 之前安全调用。"""
    try:
        c = _conn()
    except Exception:
        return 0
    try:
        cutoff = int(time.time()) - max_age_days * 86400
        c.execute("UPDATE sessions SET is_stale=1 WHERE last_updated<? AND is_stale=0", (cutoff,))
        _cleanup_stale_rows(c)
        c.commit()
        return c.total_changes
    except sqlite3.OperationalError:
        return 0
    finally:
        c.close()
