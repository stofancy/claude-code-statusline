"""balance.py 缓存逻辑测试。

覆盖场景：
  - get_balance 在失败退避（backoff）窗口内，不得返回 *其他 provider* 的旧缓存数据。
    后端切换（如 zhipu → deepseek）后的 60s 退避窗口内，旧 provider 的余额
    不应串味到新 provider 的显示上。

Run: python -m pytest tests/test_balance.py -v
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import ccs.balance as balance_mod


def _reset_mem_cache():
    balance_mod._mem_cache = None
    balance_mod._mem_cache_provider = None
    balance_mod._mem_cache_fetched = 0


def test_backoff_does_not_leak_other_provider_cache(monkeypatch, tmp_path):
    """切换 provider 后的退避窗口内，不得返回旧 provider 的缓存。

    复现：磁盘缓存是 zhipu（刚失败过，last_attempt=now，处于 60s 退避内），
    当前请求的 provider 是 deepseek。修复前 backoff 路径只校验
    ``cached_data is not None``，会把 zhipu 的数据返回给 deepseek。
    """
    _reset_mem_cache()
    now = int(time.time())

    # 磁盘缓存：zhipu，处于退避窗口内（last_attempt 刚刚），但 fetched_at 已过期
    cache_file = tmp_path / "balance_cache.json"
    cache_file.write_text(
        '{"fetched_at": %d, "last_attempt": %d, "provider": "zhipu", '
        '"data": {"provider": "zhipu", "type": "plan", "quotas": []}}'
        % (now - 9999, now),
        encoding="utf-8",
    )
    monkeypatch.setattr(balance_mod, "_CACHE_PATH", cache_file)

    # 当前 provider 解析为 deepseek
    monkeypatch.setattr(balance_mod, "provider_for", lambda mid: "deepseek")
    # 网络层不应被调用（退避中）；若被调用返回 None 以暴露问题
    monkeypatch.setattr(balance_mod, "_fetch_balance", lambda provider: None)
    monkeypatch.setenv("CCS_BALANCE_API", "1")

    result = balance_mod.get_balance("deepseek-v4-pro")

    # 修复后：provider 不匹配，退避不得返回 zhipu 数据 → 返回 None
    assert result is None or result.get("provider") == "deepseek", (
        f"退避窗口内串味返回了其他 provider 的缓存：{result!r}"
    )


def test_backoff_returns_same_provider_cache(monkeypatch, tmp_path):
    """退避窗口内，同 provider 的旧缓存仍应正常返回（退避机制本身有效）。"""
    _reset_mem_cache()
    now = int(time.time())

    cache_file = tmp_path / "balance_cache.json"
    cache_file.write_text(
        '{"fetched_at": %d, "last_attempt": %d, "provider": "deepseek", '
        '"data": {"provider": "deepseek", "type": "balance", "balance": 42.0}}'
        % (now - 9999, now),
        encoding="utf-8",
    )
    monkeypatch.setattr(balance_mod, "_CACHE_PATH", cache_file)
    monkeypatch.setattr(balance_mod, "provider_for", lambda mid: "deepseek")
    monkeypatch.setattr(balance_mod, "_fetch_balance", lambda provider: None)
    monkeypatch.setenv("CCS_BALANCE_API", "1")

    result = balance_mod.get_balance("deepseek-v4-pro")

    assert result is not None and result.get("provider") == "deepseek", (
        f"同 provider 的退避缓存应返回，实际为 {result!r}"
    )


def test_network_fail_does_not_leak_other_provider_cache(monkeypatch, tmp_path):
    """网络失败兜底路径：缓存属于其他 provider 时不得沿用，应返回 None。

    复现：磁盘缓存是 zhipu，已过期（fetched_at + last_attempt 均超窗口，
    既非 fresh 也非 backoff），当前 provider 是 deepseek，网络请求失败。
    修复前尾部 ``return cached_data`` 会把 zhipu 数据返回给 deepseek。
    """
    _reset_mem_cache()
    now = int(time.time())

    cache_file = tmp_path / "balance_cache.json"
    cache_file.write_text(
        '{"fetched_at": %d, "last_attempt": %d, "provider": "zhipu", '
        '"data": {"provider": "zhipu", "type": "plan", "quotas": []}}'
        % (now - 9999, now - 9999),
        encoding="utf-8",
    )
    monkeypatch.setattr(balance_mod, "_CACHE_PATH", cache_file)
    monkeypatch.setattr(balance_mod, "provider_for", lambda mid: "deepseek")
    monkeypatch.setattr(balance_mod, "_fetch_balance", lambda provider: None)
    monkeypatch.setenv("CCS_BALANCE_API", "1")

    result = balance_mod.get_balance("deepseek-v4-pro")

    assert result is None, (
        f"网络失败兜底串味返回了其他 provider 的缓存：{result!r}"
    )
