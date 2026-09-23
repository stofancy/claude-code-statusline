"""catalog.py：models.dev 目录压缩规则与后台刷新调度（网络以 mock 代替）。

计价优先级见 test_pricing.py，端到端效果见 test_e2e.py。

Run: python -m pytest tests/test_catalog.py -v
"""

import json
import os
import time

import pytest

import ccs.catalog as catalog_mod


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
