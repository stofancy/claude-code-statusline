"""集成测试：子代理共享 session_id 的完整流水线。

Run: .venv/bin/python tests/test_integration.py
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


def test_statusline_pipeline():
    """完整流水线：主会话 token 累积 + 子代理事件共享 session_id。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        sid = "main-session-pipeline"

        db.ensure_session(sid, "deepseek-v4-pro", "DeepSeek V4 Pro")

        # 第一轮 API 调用
        db.accumulate(sid, 10000, 500, 10000, 500, 8000, 1000)
        agg = db.get_all_totals(sid)
        assert agg["tot_input_tokens"] == 10000

        # 同一快照不重复
        db.accumulate(sid, 10000, 500, 7000, 300, 6000, 500)
        agg = db.get_all_totals(sid)
        assert agg["tot_input_tokens"] == 10000

        # 第二轮
        db.accumulate(sid, 18000, 900, 8000, 400, 7000, 300)
        agg = db.get_all_totals(sid)
        assert agg["tot_input_tokens"] == 18000

        # 子代理事件（共享 session_id）
        db.record_subagent_start(sid, "Explore", "agent-explore")
        db.record_subagent_start(sid, "Plan", "agent-plan")
        db.record_subagent_stop(sid, "Explore", "agent-explore")

        s = db.get_session(sid)
        assert s["subagent_total"] == 2
        assert s["subagent_running"] == 1

        # 工具调用
        db.record_tool_call(sid, "Read")
        db.record_tool_call(sid, "Bash")
        db.record_tool_call(sid, "Read")

        agg = db.get_all_totals(sid)
        assert agg["tool_call_count"] == 3
        assert agg["subagent_total"] == 2


def test_concurrent_sessions_isolated():
    """两个并发会话互不干扰。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        db.ensure_session("session-A", "model-A", "Model A")
        db.accumulate("session-A", 10000, 100, 10000, 100, 5000, 50)

        db.ensure_session("session-B", "model-B", "Model B")
        db.accumulate("session-B", 3000, 30, 3000, 30, 1000, 10)

        assert db.get_all_totals("session-A")["tot_input_tokens"] == 10000
        assert db.get_all_totals("session-B")["tot_input_tokens"] == 3000


def test_resume_preserves_and_continues():
    """恢复保留数据并继续累加。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        sid = "resume-sid-42"
        db.ensure_session(sid, "test", "Test")
        db.accumulate(sid, 1000, 100, 1000, 100, 500, 50)

        db._known_sessions.clear()
        db.ensure_session(sid, "test", "Test")
        db.accumulate(sid, 1500, 220, 900, 120, 600, 80)

        s = db.get_session(sid)
        assert s["tot_input_tokens"] == 1900
        assert s["tot_output_tokens"] == 220


def test_multi_model_cost_accuracy():
    """混合模型成本精确计算：pro + flash 分别定价后加总。"""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from ccs import cost as cost_mod

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        sid = "multi-model-test"
        db.ensure_session(sid, "deepseek-v4-pro", "DeepSeek V4 Pro")

        # 主会话 pro 模型
        db.accumulate(sid, 100000, 10000, 100000, 10000, 80000, 5000,
                      "deepseek-v4-pro[1m]")
        # 子代理 flash 模型
        db.accumulate(sid, 50000, 5000, 50000, 5000, 40000, 3000,
                      "deepseek-v4-flash")

        breakdown = db.get_model_breakdown(sid)
        assert "deepseek-v4-pro[1m]" in breakdown
        assert "deepseek-v4-flash" in breakdown

        # 按模型分别算成本后加总
        multi_cost = cost_mod.fmt_cost_multi(breakdown)

        # 旧方式：用最后一个 tick 的模型定价所有 token（会偏）
        old_cost = cost_mod.fmt_cost("deepseek-v4-flash", 150000, 15000)
        # 显然不同
        assert multi_cost != old_cost, \
            f"Multi-model cost should differ from single-model: {multi_cost} vs {old_cost}"


if __name__ == "__main__":
    tests = [
        test_statusline_pipeline,
        test_concurrent_sessions_isolated,
        test_resume_preserves_and_continues,
        test_multi_model_cost_accuracy,
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
