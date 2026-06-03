"""usage.py 官方用量归一化 + renderer 月度/窗口段渲染测试。

覆盖场景：
  - normalize：enterprise（窗口全 null，仅 USD extra_usage）→ monthly USD 段
  - normalize：team（five_hour/seven_day 窗口 + credits extra_usage）→ 窗口 + credits 月度
  - normalize：extra_usage 未启用 / 无数据 → monthly 为 None；全空 → 返回 None
  - _iso_to_epoch：ISO 8601 与已是 epoch 的两种输入
  - renderer：USD 月度段含 `$`，credits 月度段不含 `$`，窗口段含百分比

Run: python -m pytest tests/test_usage.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import ccs.usage as usage
import ccs.renderer as renderer


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


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


# ──────────────────────────────────────────────────────────────────────────────
# _iso_to_epoch
# ──────────────────────────────────────────────────────────────────────────────

def test_iso_to_epoch():
    assert usage._iso_to_epoch(1780516800) == 1780516800
    assert usage._iso_to_epoch("2026-06-03T20:00:00Z") == 1780516800
    assert usage._iso_to_epoch(None) is None
    assert usage._iso_to_epoch("not-a-date") is None


# ──────────────────────────────────────────────────────────────────────────────
# renderer 月度/窗口段
# ──────────────────────────────────────────────────────────────────────────────

def test_render_usd_monthly_cents_to_dollars():
    """USD 预算以美分计量：25000 == $250, 8403 == $84.03（不做 k 压缩）。"""
    seg = _strip_ansi(renderer._fmt_monthly({
        "used": 8403.0, "limit": 25000.0, "utilization": 33.6, "currency": "USD",
    }))
    assert "$84.03/$250" in seg
    assert "34%" in seg


def test_fmt_dollars_cents_conversion():
    assert renderer._fmt_dollars(25000) == "$250"
    assert renderer._fmt_dollars(8403) == "$84.03"
    assert renderer._fmt_dollars(60000) == "$600"
    assert renderer._fmt_dollars(0) == "$0"


def test_render_credits_monthly_no_dollar():
    seg = _strip_ansi(renderer._fmt_monthly({
        "used": 8176.0, "limit": 25000.0, "utilization": 32.7, "currency": None,
    }))
    assert "$" not in seg
    assert "8.18k/25k" in seg


def test_render_official_usage_combines_windows_and_monthly():
    out = _strip_ansi(renderer._fmt_official_usage({
        "windows": [
            {"label": "5H", "utilization": 24.0, "resets_at": None},
            {"label": "7D", "utilization": 41.0, "resets_at": None},
        ],
        "monthly": {"used": 100, "limit": 1000, "utilization": 10.0, "currency": None},
    }))
    assert "24%" in out and "41%" in out
    assert "100/1k" in out


def test_fmt_amount_strips_trailing_zeros():
    assert renderer._fmt_amount(25000) == "25k"
    assert renderer._fmt_amount(8176) == "8.18k"
    assert renderer._fmt_amount(1_500_000) == "1.5M"
    assert renderer._fmt_amount(500) == "500"
