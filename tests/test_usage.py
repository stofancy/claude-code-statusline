"""usage.normalize：/api/oauth/usage 两种订阅形态的响应 → 归一化结构。

覆盖场景：
  - enterprise（窗口全 null，仅 USD extra_usage）→ monthly USD
  - team（five_hour/seven_day 窗口 + credits extra_usage）→ 窗口 + credits 月度
  - extra_usage 未启用 / 无数据 → monthly 为 None；全空 → 返回 None

渲染效果由 test_e2e.py 覆盖。

Run: python -m pytest tests/test_usage.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import ccs.usage as usage


# ──────────────────────────────────────────────────────────────────────────────
# normalize
# ──────────────────────────────────────────────────────────────────────────────

def test_normalize_enterprise_usd_monthly_only():
    """enterprise：滚动窗口全 null，仅 extra_usage 以 USD 计月度预算。"""
    raw = {
        "five_hour": None,
        "seven_day": None,
        "seven_day_opus": None,
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 25000,
            "used_credits": 8176.0,
            "utilization": 32.704,
            "currency": "USD",
            "disabled_reason": None,
        },
    }
    out = usage.normalize(raw, "enterprise")
    assert out is not None
    assert out["subscription_type"] == "enterprise"
    assert out["windows"] == []
    m = out["monthly"]
    assert m["currency"] == "USD"
    assert m["used"] == 8176.0
    assert m["limit"] == 25000.0
    assert abs(m["utilization"] - 32.704) < 1e-6


def test_normalize_team_windows_and_credits():
    """team：five_hour/seven_day 有窗口；extra_usage 非 USD → credits（currency=None）。"""
    raw = {
        "five_hour": {"utilization": 23.5, "resets_at": "2026-06-03T20:00:00Z"},
        "seven_day": {"utilization": 81.2, "resets_at": "2026-06-09T00:00:00Z"},
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 25000,
            "used_credits": 8176.0,
            "utilization": 32.7,
            "currency": "credits",
        },
    }
    out = usage.normalize(raw, "team")
    labels = [w["label"] for w in out["windows"]]
    assert labels == ["5H", "7D"]
    assert out["windows"][0]["utilization"] == 23.5
    assert isinstance(out["windows"][0]["resets_at"], int)  # ISO → epoch
    assert out["monthly"]["currency"] is None  # credits 而非 USD


def test_normalize_extra_usage_disabled():
    """extra_usage 未启用 → monthly 为 None，但窗口仍保留。"""
    raw = {
        "five_hour": {"utilization": 10.0, "resets_at": 1780516800},
        "extra_usage": {"is_enabled": False, "monthly_limit": None, "currency": "USD"},
    }
    out = usage.normalize(raw, "max")
    assert out["monthly"] is None
    assert len(out["windows"]) == 1


def test_normalize_empty_returns_none():
    """无窗口且无月度 → 整体 None（无可显示内容）。"""
    assert usage.normalize({"five_hour": None, "extra_usage": None}) is None
    assert usage.normalize(None) is None
