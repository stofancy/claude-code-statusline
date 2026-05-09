"""集成测试：完整流水线与 transcript 解析。

Run: python -m pytest tests/test_integration.py -v
"""

import sys
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
    """完整流水线：session token 写入 + 子代理事件 + 工具调用。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        sid = "main-session-pipeline"

        db.ensure_session(sid, "deepseek-v4-pro", "DeepSeek V4 Pro")

        # 第一轮：写入 token 数据
        db.update_session_tokens(sid, {
            "input": 10000, "output": 500, "cache_read": 8000, "cache_write": 1000,
            "turn_count": 3,
        })
        agg = db.get_all_totals(sid)
        assert agg["tot_input_tokens"] == 10000
        assert agg["tot_cache_read_tokens"] == 8000

        # 第二轮：覆盖写入
        db.update_session_tokens(sid, {
            "input": 18000, "output": 900, "cache_read": 7000, "cache_write": 300,
            "turn_count": 6,
        })
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
        db.update_session_tokens("session-A", {
            "input": 10000, "output": 100, "cache_read": 5000, "cache_write": 50,
            "turn_count": 2,
        })

        db.ensure_session("session-B", "model-B", "Model B")
        db.update_session_tokens("session-B", {
            "input": 3000, "output": 30, "cache_read": 1000, "cache_write": 10,
            "turn_count": 1,
        })

        assert db.get_all_totals("session-A")["tot_input_tokens"] == 10000
        assert db.get_all_totals("session-B")["tot_input_tokens"] == 3000


def test_resume_preserves_and_continues():
    """恢复保留数据并继续更新。"""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        sid = "resume-sid-42"
        db.ensure_session(sid, "test", "Test")
        db.update_session_tokens(sid, {
            "input": 1000, "output": 100, "cache_read": 500, "cache_write": 50,
            "turn_count": 1,
        })

        db._known_sessions.clear()
        db.ensure_session(sid, "test", "Test")
        db.update_session_tokens(sid, {
            "input": 1500, "output": 220, "cache_read": 600, "cache_write": 80,
            "turn_count": 2,
        })

        s = db.get_session(sid)
        assert s["tot_input_tokens"] == 1500
        assert s["tot_output_tokens"] == 220


def test_multi_model_cost_accuracy():
    """混合模型成本精确计算：pro + flash 分别定价后加总。"""
    from ccs import cost as cost_mod

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        reset_state(tdp)
        db.init_db()

        sid = "multi-model-test"
        db.ensure_session(sid, "deepseek-v4-pro", "DeepSeek V4 Pro")

        db.update_model_usage(sid, {
            "deepseek-v4-pro[1m]": {"input": 100000, "output": 10000, "cache_read": 80000, "cache_write": 5000},
            "deepseek-v4-flash": {"input": 50000, "output": 5000, "cache_read": 40000, "cache_write": 3000},
        })

        breakdown = db.get_model_breakdown(sid)
        assert "deepseek-v4-pro[1m]" in breakdown
        assert "deepseek-v4-flash" in breakdown

        # 按模型分别算成本后加总
        multi_cost = cost_mod.fmt_cost_multi(breakdown)
        assert multi_cost, "cost string should not be empty"

        # 验证两种模型分别计价产生的成本不同
        pro_only = {"deepseek-v4-pro[1m]": breakdown["deepseek-v4-pro[1m]"]}
        flash_only = {"deepseek-v4-flash": breakdown["deepseek-v4-flash"]}
        assert cost_mod.fmt_cost_multi(pro_only) != cost_mod.fmt_cost_multi(flash_only), \
            "Pro and Flash should have different pricing"


def test_transcript_dedup_by_message_id():
    """按 message.id 去重：同一 UUID 只计一次，即使被 tool_result 隔开。"""
    import json
    import tempfile
    import os
    from ccs import transcript as tx_mod

    tx_mod._metrics_cache.clear()
    tx_mod._cache.clear()

    # 构造模拟 JSONL：同一 message UUID 出现多次，
    # 中间夹杂 user 消息（模拟 multi-tool-call 被 tool_result 拆分）
    msg_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    lines = [
        # streaming dup 1
        {"type": "assistant", "message": {"id": msg_id, "model": "test-model",
         "usage": {"input_tokens": 5000, "output_tokens": 100, "cache_read_input_tokens": 2000}}},
        # streaming dup 2（同 UUID 连续，应保留最后一条）
        {"type": "assistant", "message": {"id": msg_id, "model": "test-model",
         "usage": {"input_tokens": 5000, "output_tokens": 100, "cache_read_input_tokens": 2000}}},
        # tool_result（user type 打断连续链）
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        # 同 UUID 再次出现（旧逻辑会被误认为新 API 调用）
        {"type": "assistant", "message": {"id": msg_id, "model": "test-model",
         "usage": {"input_tokens": 5000, "output_tokens": 100, "cache_read_input_tokens": 2000}}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        # 又一次
        {"type": "assistant", "message": {"id": msg_id, "model": "test-model",
         "usage": {"input_tokens": 5000, "output_tokens": 100, "cache_read_input_tokens": 2000}}},
        # 另一条不同的 API 调用（不同 UUID）
        {"type": "assistant", "message": {"id": "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz", "model": "test-model",
         "usage": {"input_tokens": 1000, "output_tokens": 50}}},
    ]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
        tmp_path = f.name

    try:
        metrics = tx_mod.get_session_metrics(tmp_path)
        # 去重后应只有 2 条不同 API 调用的 token，不是 4 条
        assert metrics["input"] == 6000, \
            f"Expected 6000 (5000+1000), got {metrics['input']}"
        assert metrics["output"] == 150, \
            f"Expected 150 (100+50), got {metrics['output']}"
    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    tests = [
        test_statusline_pipeline,
        test_concurrent_sessions_isolated,
        test_resume_preserves_and_continues,
        test_multi_model_cost_accuracy,
        test_transcript_dedup_by_message_id,
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
