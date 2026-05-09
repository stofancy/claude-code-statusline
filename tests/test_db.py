"""Tests for db.py session lifecycle and token persistence.

子代理与主会话共享 session_id（官方文档确认），本测试验证该行为。

Run: python -m pytest tests/test_db.py -v
"""

import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ccs import db
import ccs.db as dbmod


def reset_state(tmpdir: Path):
    db.DB_DIR = tmpdir
    db.DB_PATH = tmpdir / "usage.db"
    db._db_inited = False
    db._known_sessions.clear()
    dbmod.DB_DIR = tmpdir
    dbmod.DB_PATH = tmpdir / "usage.db"


def test_ensure_session_new():
    """新会话：conversation_id = session_id。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("sid-001", "model-a", "Model A")
        s = db.get_session("sid-001")
        assert s["conversation_id"] == "sid-001"
        assert s["model_id"] == "model-a"
        assert s["is_stale"] == 0


def test_ensure_session_resume():
    """同一 session_id 多次调用：保留数据，更新模型，重置 is_stale。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("resume-sid", "old-model", "Old")
        db.update_session_tokens("resume-sid", {
            "input": 1000, "output": 100, "cache_read": 500, "cache_write": 50,
            "turn_count": 3,
        })
        db.record_tool_call("resume-sid", "Read")

        # 模拟被标记 stale
        c = db._conn()
        c.execute("UPDATE sessions SET is_stale=1 WHERE session_id=?", ("resume-sid",))
        c.commit()
        c.close()

        db._known_sessions.clear()
        db.ensure_session("resume-sid", "new-model", "New")

        s = db.get_session("resume-sid")
        assert s["is_stale"] == 0
        assert s["tot_input_tokens"] == 1000
        assert s["tool_call_count"] == 1
        assert s["model_id"] == "new-model"


def test_subagent_shares_session_id():
    """子代理与主会话共享 session_id：计数器在同一行更新。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("shared-sid", "pro", "Pro")

        db.record_subagent_start("shared-sid", "Explore", "agent-1")
        db.record_subagent_start("shared-sid", "general", "agent-2")
        db.record_subagent_stop("shared-sid", "Explore", "agent-1")

        s = db.get_session("shared-sid")
        assert s["subagent_total"] == 2
        assert s["subagent_running"] == 1


def test_two_sessions_isolated():
    """两个独立 session_id 各自的统计数据隔离。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("session-A", "pro", "Pro")
        db.ensure_session("session-B", "flash", "Flash")

        db.update_session_tokens("session-A", {
            "input": 5000, "output": 100, "cache_read": 3000, "cache_write": 200,
            "turn_count": 1,
        })
        db.update_session_tokens("session-B", {
            "input": 1000, "output": 50, "cache_read": 500, "cache_write": 30,
            "turn_count": 1,
        })

        assert db.get_all_totals("session-A")["tot_input_tokens"] == 5000
        assert db.get_all_totals("session-B")["tot_input_tokens"] == 1000


def test_update_session_tokens_direct_write():
    """update_session_tokens 直接覆盖写入，不累加。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("direct-write", "test", "Test")

        db.update_session_tokens("direct-write", {
            "input": 1000, "output": 100, "cache_read": 800, "cache_write": 0,
            "turn_count": 5,
        })
        s = db.get_session("direct-write")
        assert s["tot_input_tokens"] == 1000
        assert s["turn_count"] == 5

        # 新值直接覆盖，不是累加
        db.update_session_tokens("direct-write", {
            "input": 2500, "output": 220, "cache_read": 1200, "cache_write": 0,
            "turn_count": 10,
        })
        s = db.get_session("direct-write")
        assert s["tot_input_tokens"] == 2500
        assert s["turn_count"] == 10


def test_record_tool_call():
    """记录工具调用并递增计数器。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("tool-test", "test", "Test")
        db.record_tool_call("tool-test", "Read")
        db.record_tool_call("tool-test", "Bash")
        db.record_tool_call("tool-test", "Read")

        s = db.get_session("tool-test")
        assert s["tool_call_count"] == 3


def test_cleanup_stale():
    """超过 30 天的会话被清理。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("old-sid", "old", "Old")
        db.update_session_tokens("old-sid", {
            "input": 1000, "output": 100, "cache_read": 500, "cache_write": 50,
            "turn_count": 1,
        })

        c = db._conn()
        old_ts = int(time.time()) - 31 * 86400 - 60
        c.execute("UPDATE sessions SET last_updated=? WHERE session_id=?", (old_ts, "old-sid"))
        c.commit()
        c.close()

        deleted = db.cleanup_stale(max_age_days=30)
        assert deleted > 0
        assert db.get_session("old-sid") is None


def test_get_all_totals_unknown_sid():
    """未知 session_id 返回零值。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        agg = db.get_all_totals("nonexistent")
        assert agg["tot_input_tokens"] == 0


def test_update_model_usage():
    """update_model_usage 写入 model_usage 表。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("sid-1", "pro", "Pro")
        db.update_model_usage("sid-1", {
            "deepseek-v4-pro": {"input": 10000, "output": 500, "cache_read": 8000, "cache_write": 1000},
        })

        c = db._conn()
        row = c.execute(
            "SELECT * FROM model_usage WHERE session_id=? AND model_id=?",
            ("sid-1", "deepseek-v4-pro"),
        ).fetchone()
        c.close()
        assert row is not None
        assert row[2] == 10000  # tot_input_tokens


def test_model_usage_independent():
    """不同模型的用量独立存储和查询。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("sid-1", "pro", "Pro")

        db.update_model_usage("sid-1", {
            "deepseek-v4-pro": {"input": 10000, "output": 500, "cache_read": 8000, "cache_write": 1000},
            "deepseek-v4-flash": {"input": 5000, "output": 200, "cache_read": 4000, "cache_write": 300},
        })

        # 覆写 pro 模型
        db.update_model_usage("sid-1", {
            "deepseek-v4-pro": {"input": 15000, "output": 700, "cache_read": 10000, "cache_write": 1200},
        })

        breakdown = db.get_model_breakdown("sid-1")
        assert breakdown["deepseek-v4-pro"]["input"] == 15000
        assert breakdown["deepseek-v4-pro"]["cache_read"] == 10000
        assert breakdown["deepseek-v4-flash"]["input"] == 5000
        assert breakdown["deepseek-v4-flash"]["cache_read"] == 4000


def test_get_model_breakdown_empty():
    """无模型数据时返回空 dict。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("sid-1", "pro", "Pro")
        breakdown = db.get_model_breakdown("sid-1")
        assert breakdown == {}


def test_model_usage_cleanup_with_session():
    """清理会话时同步清理 model_usage。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("old-sid", "old", "Old")
        db.update_model_usage("old-sid", {
            "test-model": {"input": 1000, "output": 100, "cache_read": 500, "cache_write": 50},
        })

        c = db._conn()
        old_ts = int(time.time()) - 31 * 86400 - 60
        c.execute("UPDATE sessions SET last_updated=? WHERE session_id=?", (old_ts, "old-sid"))
        c.commit()
        c.close()

        db.cleanup_stale(max_age_days=30)
        assert db.get_model_breakdown("old-sid") == {}


if __name__ == "__main__":
    tests = [
        test_ensure_session_new,
        test_ensure_session_resume,
        test_subagent_shares_session_id,
        test_two_sessions_isolated,
        test_update_session_tokens_direct_write,
        test_record_tool_call,
        test_cleanup_stale,
        test_get_all_totals_unknown_sid,
        test_update_model_usage,
        test_model_usage_independent,
        test_get_model_breakdown_empty,
        test_model_usage_cleanup_with_session,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")

    if failed:
        sys.exit(1)
