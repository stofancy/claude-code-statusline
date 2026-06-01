"""集成测试：完整流水线与 transcript 解析。

Run: python -m pytest tests/test_integration.py -v
"""

import importlib
import os
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


def test_per_model_native_currency_display():
    """CNY 原生模型以 ¥ 显示；默认所有模型按 display_currency (CNY) 显示。

    Verifies the per-model native currency design with the new default
    ``display_currency: CNY``:

      • MiniMax (currency: CNY) shows ¥ in any config — no FX, no
        opt-in required.
      • Anthropic / DeepSeek / MiMo (no ``currency`` field) follow the
        user's ``display_currency``. With the new CNY default they are
        FX-converted from USD → CNY and render as ¥.
      • A model declaring ``currency: USD`` (with multi-currency prices)
        keeps its display in USD even when the user wants CNY — the
        model is the authoritative answer for its native currency.
      • The ``primary_model_id`` parameter keeps ``TOTAL`` and ``LAST``
        consistent on the same line in mixed breakdowns.
    """
    from ccs import cost as cost_mod

    # 1M input + 100K output for each model. Native prices:
    #   MiniMax-M3: 2.10 CNY input + 0.84 CNY output = 2.94 CNY
    #   claude-opus-4-7: $5.00 input + $2.50 output = $7.50
    usage = {"input": 1_000_000, "output": 100_000, "cache_read": 0, "cache_write": 0}

    # Save and restore CCS_CURRENCY so this test is independent of env.
    saved = os.environ.pop("CCS_CURRENCY", None)
    try:
        # ---- Default config (display == CNY) ----
        importlib.reload(cost_mod)
        # Default display is now CNY, but MiniMax has explicit `currency: CNY`,
        # so the per-model native path is taken — no FX.
        assert "¥2.94" in cost_mod.fmt_cost_multi({"MiniMax-M3": usage}), \
            "MiniMax alone should display ¥2.94 (per-model native CNY, no FX)"
        # Anthropic has no `currency` field → target = display = CNY →
        # FX from USD → CNY: 7.50 × 7.18 = 53.85
        assert "¥53.85" in cost_mod.fmt_cost_multi({"claude-opus-4-7": usage}), \
            "Anthropic with default display=CNY should FX to ¥53.85"

        # fmt_last_cost mirrors fmt_cost_multi
        assert "¥2.94" in cost_mod.fmt_last_cost("MiniMax-M3",
                                                 usage["input"], usage["output"],
                                                 usage["cache_read"], usage["cache_write"])
        assert "¥53.85" in cost_mod.fmt_last_cost("claude-opus-4-7",
                                                  usage["input"], usage["output"],
                                                  usage["cache_read"], usage["cache_write"])

        # Mixed breakdown with MiniMax as primary: TOTAL stays in ¥.
        # 2.94 + (7.50 × 7.18) = 2.94 + 53.85 = 56.79 CNY → "¥56.79"
        mixed_cny = cost_mod.fmt_cost_multi(
            {"MiniMax-M3": usage, "claude-opus-4-7": usage},
            primary_model_id="MiniMax-M3",
        )
        assert "¥56.79" in mixed_cny, f"MiniMax-primary mixed should be ¥56.79, got {mixed_cny!r}"

        # Mixed with Anthropic as primary (no `currency` field, target=display=CNY):
        # both ends land in CNY, total ¥56.79
        mixed_anth = cost_mod.fmt_cost_multi(
            {"MiniMax-M3": usage, "claude-opus-4-7": usage},
            primary_model_id="claude-opus-4-7",
        )
        assert "¥56.79" in mixed_anth, f"Anthropic-primary mixed in CNY display: {mixed_anth!r}"

        # ---- User opt-in: CCS_CURRENCY=USD → force USD display, FX all ----
        os.environ["CCS_CURRENCY"] = "USD"
        importlib.reload(cost_mod)

        # MiniMax alone: still ¥2.94 (per-model native overrides display)
        assert "¥2.94" in cost_mod.fmt_cost_multi({"MiniMax-M3": usage})

        # Anthropic alone with display=USD: target=display=USD, no FX, $7.50
        assert "$7.50" in cost_mod.fmt_cost_multi({"claude-opus-4-7": usage}), \
            "Anthropic with display=USD should show $7.50 (no FX)"

        # fmt_last_cost also follows display
        assert "$7.50" in cost_mod.fmt_last_cost("claude-opus-4-7",
                                                 usage["input"], usage["output"],
                                                 usage["cache_read"], usage["cache_write"])

        # Mixed with MiniMax primary: TOTAL in ¥ (primary wins)
        assert "¥56.79" in cost_mod.fmt_cost_multi(
            {"MiniMax-M3": usage, "claude-opus-4-7": usage},
            primary_model_id="MiniMax-M3",
        )

        # Mixed with Anthropic primary and display=USD: FX both to USD
        # 2.94/7.18 + 7.50 = 0.4095 + 7.50 = 7.91 USD → "$7.91"
        mixed_usd = cost_mod.fmt_cost_multi(
            {"MiniMax-M3": usage, "claude-opus-4-7": usage},
            primary_model_id="claude-opus-4-7",
        )
        assert "$" in mixed_usd and "¥" not in mixed_usd, \
            f"Anthropic-primary + display=USD mixed should be $, got {mixed_usd!r}"
        assert "7.91" in mixed_usd, f"Expected 7.91 in {mixed_usd!r}"

        # ---- Split between price currency and target display currency ----
        # A model that declares `currency: CNY` but only ships a `prices:`
        # block in USD: the cost is computed in USD (the only authoritative
        # block we have) and FX-converted into CNY for display, so the user
        # sees ¥ — never a USD value with a ¥ symbol.
        fake_pricing = {
            "base_currency": "USD",
            "display_currency": "CNY",
            "fx_rates": {"USD": 1.0, "CNY": 7.18},
            "providers": {
                "fake": {
                    "cny-native-only-usd-priced": {
                        "currency": "CNY",
                        "prices": {
                            "USD": {"input_per_1m": 0.30, "output_per_1m": 1.20},
                        },
                    },
                },
            },
        }
        original_load = cost_mod._load_pricing
        cost_mod._load_pricing = lambda: fake_pricing
        try:
            # 1M input + 100K output @ USD prices:
            #   0.30 + 0.12 = 0.42 USD → FX to CNY: 0.42 × 7.18 = 3.0156 → ¥3.02
            rendered = cost_mod.fmt_cost_multi(
                {"cny-native-only-usd-priced": usage},
                primary_model_id="cny-native-only-usd-priced",
            )
            assert "¥" in rendered, f"CNY-native model should display ¥, got {rendered!r}"
            assert "3.02" in rendered or "3.01" in rendered, \
                f"expected FX-converted ¥3.02, got {rendered!r}"
            # And ¥ must be from FX, not a no-op round trip: if FX were
            # skipped, the value would be $0.42, not ¥3.02.
            assert "0.42" not in rendered and "0.420" not in rendered, \
                f"value should be FX-converted to CNY, got {rendered!r}"

            rendered = cost_mod.fmt_last_cost("cny-native-only-usd-priced",
                                              usage["input"], usage["output"],
                                              usage["cache_read"], usage["cache_write"])
            assert "¥" in rendered, f"LAST for CNY-native should be ¥, got {rendered!r}"
        finally:
            cost_mod._load_pricing = original_load
            importlib.reload(cost_mod)

        # ---- USD-native model wins over user display preference ----
        # A model declaring `currency: USD` keeps its display in USD even
        # when the user has set `display_currency: CNY`. The model has the
        # authoritative answer for what currency it is.
        fake_pricing2 = {
            "base_currency": "USD",
            "display_currency": "CNY",
            "fx_rates": {"USD": 1.0, "CNY": 7.18},
            "providers": {
                "fake2": {
                    "usd-native-multi-currency": {
                        "currency": "USD",
                        "prices": {
                            "USD": {"input_per_1m": 0.50, "output_per_1m": 2.00},
                            "CNY": {"input_per_1m": 3.59, "output_per_1m": 14.36},
                        },
                    },
                },
            },
        }
        cost_mod._load_pricing = lambda: fake_pricing2
        try:
            # 0.50 + 0.20 = 0.70 USD → "$0.700" (USD-native wins)
            rendered = cost_mod.fmt_cost_multi(
                {"usd-native-multi-currency": usage},
                primary_model_id="usd-native-multi-currency",
            )
            assert "$0.700" in rendered, f"USD-native model in $: {rendered!r}"

            # Even with display=USD explicit, USD-native model stays in $.
            os.environ["CCS_CURRENCY"] = "USD"
            importlib.reload(cost_mod)
            cost_mod._load_pricing = lambda: fake_pricing2
            rendered = cost_mod.fmt_cost_multi(
                {"usd-native-multi-currency": usage},
                primary_model_id="usd-native-multi-currency",
            )
            assert "$" in rendered and "¥" not in rendered, \
                f"USD-native model should stay $ even with display=USD, got {rendered!r}"
        finally:
            cost_mod._load_pricing = original_load
            os.environ.pop("CCS_CURRENCY", None)
            importlib.reload(cost_mod)

    finally:
        if saved is not None:
            os.environ["CCS_CURRENCY"] = saved
        else:
            os.environ.pop("CCS_CURRENCY", None)
        importlib.reload(cost_mod)


def test_cache_suffix_stripping():
    """[1m] / [1M] / 嵌套后缀都应能命中 MiniMax-M3。

    Real-world: Claude Code may emit ``MiniMax-M3[1M][1m]`` when the
    1-hour cache variant is requested on top of the default 5-min one.
    The resolver must strip both case-insensitively and keep stripping
    until no more ``[1m]`` suffix remains.
    """
    from ccs import cost as cost_mod

    usage = {"input": 1_000_000, "output": 100_000, "cache_read": 0, "cache_write": 0}

    # Every variant must resolve to MiniMax-M3 and render ¥ in the
    # default config (display=CNY, MiniMax has explicit `currency: CNY`).
    for variant in [
        "MiniMax-M3",
        "MiniMax-M3[1m]",
        "MiniMax-M3[1M]",            # capital M
        "MiniMax-M3[1M][1m]",          # doubled, capital + lowercase
        "minimax-m3",                  # all lowercase
        "minimax-m3[1m]",              # lowercase + suffix
    ]:
        rendered = cost_mod.fmt_last_cost(variant,
                                          usage["input"], usage["output"],
                                          usage["cache_read"], usage["cache_write"])
        assert "¥" in rendered, f"{variant!r} should render ¥, got {rendered!r}"
        assert rendered == "¥2.94", f"{variant!r} should be ¥2.94, got {rendered!r}"


if __name__ == "__main__":
    tests = [
        test_statusline_pipeline,
        test_concurrent_sessions_isolated,
        test_resume_preserves_and_continues,
        test_multi_model_cost_accuracy,
        test_transcript_dedup_by_message_id,
        test_per_model_native_currency_display,
        test_cache_suffix_stripping,
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
