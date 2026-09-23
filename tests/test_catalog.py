"""catalog.py（models.dev 价格目录 + 每日汇率）及其与 cost.py 分层定价的测试。

Run: python -m pytest tests/test_catalog.py -v
"""

import json
import os
import time

import pytest

import ccs.catalog as catalog_mod
import ccs.cost as cost_mod


RAW = {
    "anthropic": {"models": {
        "claude-opus-5-5": {"cost": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5}},
        "claude-opus-5": {"cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}},
    }},
    "xai": {"models": {
        "grok-4.6": {"cost": {"input": 2, "output": 6, "cache_read": 0.5,
                              "tiers": [{"input": 4, "output": 12, "cache_read": 1,
                                         "tier": {"type": "context", "size": 200000}}],
                              "context_over_200k": {"input": 4, "output": 12, "cache_read": 1}}},
    }},
    "google": {"models": {
        "gemini-x": {"cost": {"input": 1, "output": 2,
                              "context_over_200k": {"input": 3, "output": 4}}},
    }},
    "minimax": {"models": {
        "MiniMax-M3": {"cost": {"input": 0.3, "output": 1.2, "cache_read": 0.06}},
    }},
    "openrouter": {"models": {
        "anthropic/claude-opus-5-5": {"cost": {"input": 99, "output": 99}},
        "qwen/qwen3-max": {"cost": {"input": 1.2, "output": 6}},
    }},
    "zzz-reseller": {"models": {
        "qwen3-max": {"cost": {"input": 50, "output": 50}},
        "free-thing": {"cost": {"input": 0, "output": 0}},
        "no-cost": {"name": "x"},
    }},
    "zhipuai-coding-plan": {"models": {
        "glm-9": {"cost": {"input": 0, "output": 0}},
    }},
}


def _install_catalog(models: dict | None = None, rates: dict | None = None) -> None:
    if models is not None:
        catalog_mod._write_json(catalog_mod._MODELS_PATH, {"fetched_at": 0, "models": models})
    if rates is not None:
        catalog_mod._write_json(catalog_mod._FX_PATH, {"fetched_at": 0, "rates": rates})


def _resolve(model_id, prompt_tokens=None):
    return cost_mod._resolve_price(model_id, "USD", "USD", prompt_tokens=prompt_tokens)


# ── compact ───────────────────────────────────────────────────────────────

def test_compact_uses_author_catalogs_only():
    m = catalog_mod.compact(RAW)
    assert m["claude-opus-5-5"]["input_per_1m"] == 4.0
    assert m["claude-opus-5-5"]["cache_write_per_1m"] == 5.0
    assert "minimax-m3" in m  # 键已 casefold
    assert "qwen3-max" not in m and "glm-9" not in m  # 转售 / 订阅目录被忽略


def test_compact_tiers_and_legacy_context_over_200k():
    m = catalog_mod.compact(RAW)
    assert m["grok-4.6"]["tiers"] == [
        {"input_per_1m": 4.0, "output_per_1m": 12.0, "cache_read_per_1m": 1.0, "above": 200000}]
    assert m["gemini-x"]["tiers"] == [{"input_per_1m": 3.0, "output_per_1m": 4.0, "above": 200000}]


# ── lookup / layering in cost._resolve_price ──────────────────────────────

def test_new_model_from_catalog_beats_prefix_stripped_yaml():
    """claude-opus-5-5 不在 YAML：旧行为剥离到 claude-opus-5（$5/$25），
    现在 models.dev 的精确条目优先。"""
    _install_catalog(catalog_mod.compact(RAW))
    price, cur, target = _resolve("claude-opus-5-5[1m]")
    assert price["input_per_1m"] == 4.0
    assert (cur, target) == ("USD", "USD")


def test_without_catalog_falls_back_to_builtin_yaml():
    price, _, _ = _resolve("claude-opus-5-5")
    assert price["input_per_1m"] == 5.00  # 剥离到内置 claude-opus-5


def test_catalog_beats_plain_builtin_entry_for_same_id():
    _install_catalog(catalog_mod.compact(RAW))
    price, _, _ = _resolve("claude-opus-5")
    assert price["input_per_1m"] == 3.0


def test_pinned_builtin_entry_beats_catalog():
    """MiniMax-M3 在内置表中声明 currency: CNY —— 原生人民币价不被 models.dev 覆盖。"""
    _install_catalog(catalog_mod.compact(RAW))
    _, cur, target = cost_mod._resolve_price("MiniMax-M3", "USD", "USD")
    assert target == "CNY"


def test_user_yaml_entry_beats_catalog_and_keeps_builtin(tmp_path):
    _install_catalog(catalog_mod.compact(RAW))
    cost_mod._USER_PRICING.parent.mkdir(parents=True, exist_ok=True)
    cost_mod._USER_PRICING.write_text(
        "providers:\n  mine:\n    claude-opus-5-5:\n      input_per_1m: 7\n      output_per_1m: 8\n",
        encoding="utf-8")
    price, _, _ = _resolve("claude-opus-5-5")
    assert price["input_per_1m"] == 7
    # 用户文件只写了一个模型，内置表其余条目依旧可用
    price, _, _ = _resolve("deepseek-chat")
    assert price is not cost_mod._DEFAULT_PRICING


def test_pricing_api_disabled_ignores_catalog(monkeypatch):
    _install_catalog(catalog_mod.compact(RAW))
    monkeypatch.setenv("CCS_PRICING_API", "0")
    price, _, _ = _resolve("claude-opus-5-5")
    assert price["input_per_1m"] == 5.00


def test_catalog_price_is_usd_even_with_non_usd_base():
    _install_catalog(catalog_mod.compact(RAW))
    _, cur, target = cost_mod._resolve_price("claude-opus-5-5", "CNY", "CNY")
    assert (cur, target) == ("USD", "CNY")


# ── context-length tiers ──────────────────────────────────────────────────

def test_builtin_tier_selected_by_prompt_size():
    below, _, _ = _resolve("grok-4.6", prompt_tokens=200_000)
    above, _, _ = _resolve("grok-4.6", prompt_tokens=200_001)
    assert below["input_per_1m"] == 2.00
    assert above["input_per_1m"] == 4.00
    assert above["cache_read_per_1m"] == 1.00
    assert "tiers" not in above


def test_tier_replaces_whole_alias_group():
    price = {"input_per_1m": 1, "input_cache_hit_per_1m": 0.1, "output_per_1m": 2,
             "tiers": [{"above": 10, "input_per_1m": 3, "cache_read_per_1m": 0.3}]}
    out = cost_mod._select_tier_block(price, 11)
    assert out["cache_read_per_1m"] == 0.3
    assert "input_cache_hit_per_1m" not in out
    assert out["output_per_1m"] == 2


def test_tier_picks_highest_threshold_below_prompt():
    price = {"input_per_1m": 1, "output_per_1m": 1,
             "tiers": [{"above": 128000, "input_per_1m": 3}, {"above": 32000, "input_per_1m": 2}]}
    assert cost_mod._select_tier_block(price, 50_000)["input_per_1m"] == 2
    assert cost_mod._select_tier_block(price, 200_000)["input_per_1m"] == 3
    assert cost_mod._select_tier_block(price, None) is price


def test_fmt_cost_multi_prices_each_call_at_its_own_tier(monkeypatch):
    monkeypatch.setenv("CCS_CURRENCY", "USD")
    small = {"input": 100_000, "output": 0, "cache_read": 0, "cache_write": 0}
    big = {"input": 300_000, "output": 0, "cache_read": 0, "cache_write": 0}
    out = cost_mod.fmt_cost_multi({}, model_calls={"grok-4.6": [("", small), ("", big)]})
    # 0.1M × $2 + 0.3M × $4 = $1.40
    assert out == "$1.40"


def test_fmt_last_cost_uses_tier(monkeypatch):
    monkeypatch.setenv("CCS_CURRENCY", "USD")
    # prompt = 150k + 100k cache_read = 250k > 200k → $4 / $1
    out = cost_mod.fmt_last_cost("grok-4.6", 150_000, 0, 100_000)
    assert out == "$0.700"


# ── FX ────────────────────────────────────────────────────────────────────

def test_fetched_fx_overrides_builtin_snapshot():
    _install_catalog(rates={"USD": 1.0, "CNY": 6.5})
    _, _, fx = cost_mod._currency_settings()
    assert fx["CNY"] == 6.5
    assert fx["TWD"] == 32.3  # 未抓取到的币种沿用内置快照


def test_user_fx_rates_pin_over_fetched():
    _install_catalog(rates={"USD": 1.0, "CNY": 6.5})
    cost_mod._USER_PRICING.parent.mkdir(parents=True, exist_ok=True)
    cost_mod._USER_PRICING.write_text("fx_rates:\n  CNY: 7.0\n", encoding="utf-8")
    _, _, fx = cost_mod._currency_settings()
    assert fx["CNY"] == 7.0


# ── refresh scheduling ────────────────────────────────────────────────────

def test_maybe_refresh_spawns_once_then_backs_off(monkeypatch):
    spawned = []
    monkeypatch.setattr(catalog_mod, "_spawn_refresh", lambda: spawned.append(1))
    assert catalog_mod.maybe_refresh() is True
    assert catalog_mod.maybe_refresh() is False  # lock 1h backoff
    assert len(spawned) == 1


def test_maybe_refresh_skips_when_fresh(monkeypatch):
    _install_catalog({}, {"USD": 1.0, "CNY": 7})
    spawned = []
    monkeypatch.setattr(catalog_mod, "_spawn_refresh", lambda: spawned.append(1))
    assert catalog_mod.maybe_refresh() is False
    assert spawned == []


def test_maybe_refresh_after_ttl(monkeypatch):
    _install_catalog({}, {"USD": 1.0, "CNY": 7})
    old = time.time() - 2 * 86400
    os.utime(catalog_mod._MODELS_PATH, (old, old))
    monkeypatch.setattr(catalog_mod, "_spawn_refresh", lambda: None)
    assert catalog_mod.maybe_refresh() is True


def test_maybe_refresh_disabled(monkeypatch):
    monkeypatch.setenv("CCS_PRICING_API", "0")
    assert catalog_mod.maybe_refresh() is False


# ── network refresh (HTTP mocked) ─────────────────────────────────────────

class _Headers(dict):
    def get(self, k, default=None):
        return super().get(k, default)


def test_refresh_models_writes_etag_then_uses_304(monkeypatch):
    calls = []

    def fake_get(url, headers=None):
        calls.append(headers)
        if headers and headers.get("If-None-Match") == '"abc"':
            return 304, b"", _Headers()
        return 200, json.dumps(RAW).encode(), _Headers(ETag='"abc"')

    monkeypatch.setattr(catalog_mod, "_http_get", fake_get)
    assert catalog_mod.refresh_models() is True
    first = json.loads(catalog_mod._MODELS_PATH.read_text())
    assert first["etag"] == '"abc"'
    assert "claude-opus-5-5" in first["models"]

    old = time.time() - 2 * 86400
    os.utime(catalog_mod._MODELS_PATH, (old, old))
    assert catalog_mod.refresh_models() is True
    assert calls[-1] == {"If-None-Match": '"abc"'}
    assert not catalog_mod._stale(catalog_mod._MODELS_PATH)
    assert json.loads(catalog_mod._MODELS_PATH.read_text())["models"] == first["models"]


def test_refresh_reports_failure_without_raising(monkeypatch):
    def boom(url, headers=None):
        raise OSError("offline")

    monkeypatch.setattr(catalog_mod, "_http_get", boom)
    assert catalog_mod.refresh() == {"models": False, "fx": False}
