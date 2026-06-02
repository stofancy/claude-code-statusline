"""cost.py 定价解析与成本计算测试。

覆盖场景：
  - _resolve_price 对 claude-opus-4-8 和 claude-opus-4-8[1m] 均返回 $5/$25 定价块
  - 回归保护：claude-opus-4-8 不退化匹配到 claude-opus-4（$15/$75）
  - fmt_cost_multi 对 opus-4-8 usage breakdown 计算结果使用 ¥ 符号（display_currency=CNY）且数量级正确

Run: python -m pytest tests/test_cost.py -v
"""

import sys
import os
from pathlib import Path

# 将 src 目录加入路径，确保能直接 import ccs
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import ccs.cost as cost_mod


# ──────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────────────────────

def _resolve(model_id: str) -> tuple[dict, str, str]:
    """调用 _resolve_price，使用内置定价表的默认货币（USD）。"""
    return cost_mod._resolve_price(model_id, "USD", "USD")


# ──────────────────────────────────────────────────────────────────────────────
# 任务 (a)：claude-opus-4-8 与 claude-opus-4-8[1m] 均解析到 $5/$25 定价块
# ──────────────────────────────────────────────────────────────────────────────

def test_opus_4_8_exact_match_pricing():
    """claude-opus-4-8 精确匹配应返回 input=$5、output=$25 的定价块。"""
    price, price_currency, _ = _resolve("claude-opus-4-8")
    assert price_currency == "USD", f"预期价格货币为 USD，实际为 {price_currency!r}"
    assert price.get("input_per_1m") == 5.00, (
        f"claude-opus-4-8 input 应为 $5.00，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 25.00, (
        f"claude-opus-4-8 output 应为 $25.00，实际为 {price.get('output_per_1m')}"
    )
    assert price.get("cache_read_per_1m") == 0.50, (
        f"claude-opus-4-8 cache_read 应为 $0.50，实际为 {price.get('cache_read_per_1m')}"
    )


def test_opus_4_8_1m_suffix_stripped_to_correct_pricing():
    """claude-opus-4-8[1m]（1M 上下文窗口标识）应剥离后缀并匹配到相同的 $5/$25 定价块。

    这是修复的核心场景：_CACHE_SUFFIX_RE 剥掉 [1m] 后得到 claude-opus-4-8，
    精确命中定价表，而非逐段退化到 claude-opus-4 的 $15/$75 旧价。
    """
    price, price_currency, _ = _resolve("claude-opus-4-8[1m]")
    assert price_currency == "USD", f"预期价格货币为 USD，实际为 {price_currency!r}"
    assert price.get("input_per_1m") == 5.00, (
        f"claude-opus-4-8[1m] input 应为 $5.00，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 25.00, (
        f"claude-opus-4-8[1m] output 应为 $25.00，实际为 {price.get('output_per_1m')}"
    )
    assert price.get("cache_read_per_1m") == 0.50, (
        f"claude-opus-4-8[1m] cache_read 应为 $0.50，实际为 {price.get('cache_read_per_1m')}"
    )


def test_opus_4_8_uppercase_1m_suffix_stripped():
    """claude-opus-4-8[1M]（大写后缀）同样能正确剥离并匹配 $5/$25 定价。"""
    price, _, _ = _resolve("claude-opus-4-8[1M]")
    assert price.get("input_per_1m") == 5.00, (
        f"大写后缀 [1M] 应被剥离，input 期望 $5.00，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 25.00, (
        f"大写后缀 [1M] 应被剥离，output 期望 $25.00，实际为 {price.get('output_per_1m')}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 任务 (b)：回归保护——claude-opus-4-8 不退化到 claude-opus-4 的 $15/$75
# ──────────────────────────────────────────────────────────────────────────────

def test_opus_4_8_does_not_fallback_to_opus_4_legacy_pricing():
    """回归保护：claude-opus-4-8 不应匹配 claude-opus-4 的历史 $15/$75 定价。

    修复前的错误路径：
      claude-opus-4-8[1m]
        → 无精确匹配
        → 逐段剥离：claude-opus-4-8 → claude-opus-4  ← 错误命中
        → 返回 input=$15、output=$75（约 3 倍高估）
    """
    for model_id in ("claude-opus-4-8", "claude-opus-4-8[1m]", "claude-opus-4-8[1M]"):
        price, _, _ = _resolve(model_id)
        assert price.get("input_per_1m") != 15.00, (
            f"{model_id!r} 退化匹配到了旧版 claude-opus-4 的 $15 定价，"
            f"应为 $5.00（bug 回归）"
        )
        assert price.get("output_per_1m") != 75.00, (
            f"{model_id!r} 退化匹配到了旧版 claude-opus-4 的 $75 定价，"
            f"应为 $25.00（bug 回归）"
        )


def test_opus_4_legacy_pricing_still_works():
    """确认 claude-opus-4 自身仍保留 $15/$75 历史定价（不应被修复误伤）。"""
    price, _, _ = _resolve("claude-opus-4")
    assert price.get("input_per_1m") == 15.00, (
        f"claude-opus-4 本身应保留 $15 定价，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 75.00, (
        f"claude-opus-4 本身应保留 $75 定价，实际为 {price.get('output_per_1m')}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 任务 (c)：fmt_cost_multi 对 opus-4-8 breakdown 计算，符号与数量级验证
# ──────────────────────────────────────────────────────────────────────────────

def test_fmt_cost_multi_opus_4_8_symbol_and_magnitude():
    """fmt_cost_multi 对 claude-opus-4-8 usage 计算：显示 ¥ 符号，数量级正确。

    定价表 display_currency=CNY，USD→CNY fx=7.18。
    用量设置：input=1_000_000 tokens（$5.00）+ output=100_000 tokens（$2.50）
    预期总成本 USD $7.50 → CNY ¥53.85（≈ 7.50 × 7.18）。
    若误用 $15/$75 定价则得 USD $22.50 → CNY ¥161.55（约 3 倍高估）。
    """
    breakdown = {
        "claude-opus-4-8": {
            "input": 1_000_000,
            "output": 100_000,
            "cache_read": 0,
            "cache_write": 0,
        }
    }
    result = cost_mod.fmt_cost_multi(breakdown)
    # display_currency=CNY，必须以 ¥ 开头
    assert result.startswith("¥"), (
        f"claude-opus-4-8 成本应以 ¥ 显示（display_currency=CNY），实际为 {result!r}"
    )
    # 提取数值部分进行数量级断言
    numeric_str = result.lstrip("¥")
    value = float(numeric_str)
    # 正确定价：¥53.85；回归定价：¥161.55。取中间值 ¥90 作为上界。
    assert value < 90.0, (
        f"claude-opus-4-8 成本估算偏高（{result!r}），"
        f"预期约 ¥53.85，疑似回归到了旧版 $15/$75 定价"
    )
    # 也断言不低于合理下界（防止意外的零定价）
    assert value > 30.0, (
        f"claude-opus-4-8 成本估算偏低（{result!r}），预期约 ¥53.85"
    )


def test_fmt_cost_multi_opus_4_8_1m_suffix_symbol_and_magnitude():
    """fmt_cost_multi 对 claude-opus-4-8[1m]（1M 上下文窗口变体）同样计算正确。

    与上一个测试完全相同的用量，验证 [1m] 后缀剥离在 fmt_cost_multi 调用链中也生效。
    display_currency=CNY，预期约 ¥53.85。
    """
    breakdown = {
        "claude-opus-4-8[1m]": {
            "input": 1_000_000,
            "output": 100_000,
            "cache_read": 0,
            "cache_write": 0,
        }
    }
    result = cost_mod.fmt_cost_multi(breakdown)
    # display_currency=CNY，必须以 ¥ 开头
    assert result.startswith("¥"), (
        f"claude-opus-4-8[1m] 成本应以 ¥ 显示（display_currency=CNY），实际为 {result!r}"
    )
    numeric_str = result.lstrip("¥")
    value = float(numeric_str)
    # 正确定价：¥53.85；回归定价：¥161.55。取 ¥90 为上界区分两者。
    assert value < 90.0, (
        f"claude-opus-4-8[1m] 成本偏高（{result!r}），疑似退化到旧版定价（bug 回归）"
    )
    assert value > 30.0, (
        f"claude-opus-4-8[1m] 成本偏低（{result!r}），预期约 ¥53.85"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 任务 (d)：真实 /usage 用量成本核对（CNY 显示）
# ──────────────────────────────────────────────────────────────────────────────

def test_fmt_cost_multi_real_usage_cny_verification():
    """用真实 /usage 数据对 claude-opus-4-8[1m] 做端到端成本核对（display_currency=CNY）。

    用量来源：实测 transcript usage 字段（2026-06-02 会话）：
      input=366, output=19900, cache_read=466200, cache_write=32500

    手工计算（USD）：
      input:       366 / 1e6 × $5.00   = $0.001830
      output:   19900 / 1e6 × $25.00  = $0.497500
      cache_read: 466200 / 1e6 × $0.50 = $0.233100
      cache_write: 32500 / 1e6 × $6.25 = $0.203125
      合计 USD：$0.935555
      合计 CNY：$0.935555 × 7.18 = ¥6.717

    断言：fmt_cost_multi 结果以 ¥ 开头，数值在 ¥6.5 ~ ¥7.0 之间（含浮点容差）。
    """
    breakdown = {
        "claude-opus-4-8[1m]": {
            "input": 366,
            "output": 19900,
            "cache_read": 466200,
            "cache_write": 32500,
        }
    }
    result = cost_mod.fmt_cost_multi(breakdown, "claude-opus-4-8[1m]")
    # display_currency=CNY
    assert result.startswith("¥"), (
        f"真实用量成本应以 ¥ 显示（display_currency=CNY），实际为 {result!r}"
    )
    numeric_str = result.lstrip("¥")
    value = float(numeric_str)
    # 手工预期 ≈ ¥6.717；允许 ±0.10 的浮点/取整容差
    assert 6.5 <= value <= 7.0, (
        f"claude-opus-4-8[1m] 真实用量成本核对失败：得到 {result!r}，"
        f"预期约 ¥6.72（USD $0.9356 × fx 7.18）。"
        f"若数值约 ¥20.x，说明退化到旧版 $15/$75 定价（bug 回归）。"
    )
