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


def test_opus_4_8_dot_form_resolves_to_correct_pricing():
    """claude-opus-4.8（点号形式）应归一化为 claude-opus-4-8 并命中 $5/$25 定价块。

    根因：Claude Code 在 transcript 的 message.model 里把模型名写成点号形式
    （claude-opus-4.8、claude-haiku-4.5，实测占 99% 条目），而 pricing.yaml
    的键全是连字符形式（claude-opus-4-8）。修复前候选生成只按 '-' 分段：
      claude-opus-4.8 → claude-opus → claude  ← 永远到不了 claude-opus-4-8
    于是落入 _DEFAULT_PRICING（$1/$4/$0.1 且无 cache_write），低估近一个数量级。
    """
    price, price_currency, _ = _resolve("claude-opus-4.8")
    assert price_currency == "USD", f"预期价格货币为 USD，实际为 {price_currency!r}"
    assert price.get("input_per_1m") == 5.00, (
        f"claude-opus-4.8 input 应为 $5.00，实际为 {price.get('input_per_1m')}（落入默认价 = bug）"
    )
    assert price.get("output_per_1m") == 25.00, (
        f"claude-opus-4.8 output 应为 $25.00，实际为 {price.get('output_per_1m')}"
    )
    assert price.get("cache_write_per_1m") == 6.25, (
        f"claude-opus-4.8 应有 cache_write=$6.25，实际为 {price.get('cache_write_per_1m')}"
    )


def test_haiku_4_5_dot_form_resolves_to_correct_pricing():
    """claude-haiku-4.5（点号形式）应归一化为 claude-haiku-4-5 并命中真实定价块。

    注意：haiku 的真实 input 价恰为 $1.00，与默认价 input 撞值，不能用 input
    判别。改用默认价不具备的特征字段：output=$5.00（默认 $4.00）且存在
    cache_write_per_1m=$1.25（默认价无此字段）。
    """
    price, _, _ = _resolve("claude-haiku-4.5")
    assert price.get("output_per_1m") == 5.00, (
        f"claude-haiku-4.5 output 应为 $5.00，实际为 {price.get('output_per_1m')}"
        f"（落入默认价 $4.00 = bug）"
    )
    assert price.get("cache_write_per_1m") == 1.25, (
        f"claude-haiku-4.5 应有 cache_write=$1.25，默认价无此字段，"
        f"实际为 {price.get('cache_write_per_1m')}（bug）"
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


# ──────────────────────────────────────────────────────────────────────────────
# 任务 (e)：代理前缀模型名解析（openrouter、opencode 等）
# ──────────────────────────────────────────────────────────────────────────────

def test_openrouter_anthropic_claude_resolves_to_opus_4_8_pricing():
    """openrouter/anthropic/claude-opus-4-8 应剥离前缀后命中内置 opus-4-8 定价 ($5/$25)。

    解析链：openrouter/anthropic/claude-opus-4-8
      → 剥 /: anthropic/claude-opus-4-8 (不命中)
      → 剥 /: claude-opus-4-8 (命中) → input=$5, output=$25
    """
    price, price_currency, _ = _resolve("openrouter/anthropic/claude-opus-4-8")
    assert price_currency == "USD", f"预期价格货币为 USD，实际为 {price_currency!r}"
    assert price.get("input_per_1m") == 5.00, (
        f"openrouter/anthropic/claude-opus-4-8 应解析为 $5 input，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 25.00, (
        f"openrouter/anthropic/claude-opus-4-8 应解析为 $25 output，实际为 {price.get('output_per_1m')}"
    )


def test_openrouter_deepseek_with_provider_segment_resolves():
    """openrouter/deepseek/deepseek-v4-pro 嵌入 provider 段也应命中 deepseek 定价。"""
    price, _, _ = _resolve("openrouter/deepseek/deepseek-v4-pro")
    assert price.get("input_per_1m") == 3.0, (
        f"openrouter/deepseek/deepseek-v4-pro 应解析为 ¥3 input，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 6.0, (
        f"openrouter/deepseek/deepseek-v4-pro 应解析为 ¥6 output，实际为 {price.get('output_per_1m')}"
    )


def test_openrouter_deepseek_no_provider_segment_resolves():
    """openrouter/deepseek-v4-pro（无 provider 段）也应命中 deepseek 定价。"""
    price, _, _ = _resolve("openrouter/deepseek-v4-pro")
    assert price.get("input_per_1m") == 3.0, (
        f"openrouter/deepseek-v4-pro 应解析为 ¥3 input，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 6.0, (
        f"openrouter/deepseek-v4-pro 应解析为 ¥6 output，实际为 {price.get('output_per_1m')}"
    )


def test_opencode_go_prefix_stripped():
    """opencode-go/<model> 形式（含连字符变体）应被剥离为真实模型。"""
    price, _, _ = _resolve("opencode-go/deepseek-v4-pro")
    assert price.get("input_per_1m") == 3.0, (
        f"opencode-go/deepseek-v4-pro 应解析为 ¥3 input，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 6.0, (
        f"opencode-go/deepseek-v4-pro 应解析为 ¥6 output，实际为 {price.get('output_per_1m')}"
    )


def test_opencode_prefix_stripped():
    """opencode/<model> 形式也应被剥离。"""
    price, _, _ = _resolve("opencode/claude-sonnet-4-6")
    assert price.get("input_per_1m") == 3.00, (
        f"opencode/claude-sonnet-4-6 应解析为 $3 input，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 15.00, (
        f"opencode/claude-sonnet-4-6 应解析为 $15 output，实际为 {price.get('output_per_1m')}"
    )


def test_weihub_glm_5_2_exact_proxy_id_pricing():
    """weihub/glm-5.2 应命中完整代理 ID 条目，避免落入默认定价。"""
    price, price_currency, target = _resolve("weihub/glm-5.2")
    assert price_currency == "CNY"
    assert target == "CNY"
    assert price.get("input_per_1m") == 8.00, (
        f"weihub/glm-5.2 input 应为 ¥8，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("input_cache_hit_per_1m") == 1.60, (
        f"weihub/glm-5.2 cache hit 应为 ¥1.6，实际为 {price.get('input_cache_hit_per_1m')}"
    )
    assert price.get("output_per_1m") == 28.00, (
        f"weihub/glm-5.2 output 应为 ¥28，实际为 {price.get('output_per_1m')}"
    )


def test_weihub_kimi_k2_7_code_exact_proxy_id_pricing():
    """weihub/kimi-k2.7-code 应使用 WeiHub/Kimi 代理专属价表。"""
    price, price_currency, target = _resolve("weihub/kimi-k2.7-code")
    assert price_currency == "CNY"
    assert target == "CNY"
    assert price.get("input_per_1m") == 6.50, (
        f"weihub/kimi-k2.7-code input 应为 ¥6.5，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("input_cache_hit_per_1m") == 1.30, (
        f"weihub/kimi-k2.7-code cache hit 应为 ¥1.3，实际为 {price.get('input_cache_hit_per_1m')}"
    )
    assert price.get("output_per_1m") == 27.00, (
        f"weihub/kimi-k2.7-code output 应为 ¥27，实际为 {price.get('output_per_1m')}"
    )


def test_fmt_cost_multi_weihub_glm_5_2_symbol_and_magnitude():
    """weihub/glm-5.2 cost 应按 CNY 直接计算并显示 ¥。"""
    breakdown = {
        "weihub/glm-5.2": {
            "input": 1_000_000,
            "output": 100_000,
            "cache_read": 100_000,
            "cache_write": 0,
        }
    }
    result = cost_mod.fmt_cost_multi(breakdown)
    assert result.startswith("¥"), f"WeiHub GLM 成本应显示 ¥，实际为 {result!r}"
    value = float(result.lstrip("¥"))
    assert 10.5 < value < 11.5, (
        f"WeiHub GLM 成本预期约 ¥10.96，实际为 {result!r}"
    )


def test_openrouter_with_1m_suffix_resolves_correctly():
    """[1m] 后缀与代理前缀组合：openrouter/anthropic/claude-opus-4-8[1m] 应仍命中 $5/$25。

    这是关键组合：[1m] 剥离在先（去掉后缀），/ 剥离在后（去掉前缀）。
    旧代码会因前缀不匹配而走默认 $1/$4 定价。
    """
    price, _, _ = _resolve("openrouter/anthropic/claude-opus-4-8[1m]")
    assert price.get("input_per_1m") == 5.00, (
        f"openrouter/anthropic/claude-opus-4-8[1m] 应解析为 $5 input，"
        f"实际为 {price.get('input_per_1m')}（可能走了默认定价 $1）"
    )
    assert price.get("output_per_1m") == 25.00, (
        f"openrouter/anthropic/claude-opus-4-8[1m] 应解析为 $25 output，"
        f"实际为 {price.get('output_per_1m')}（可能走了默认定价 $4）"
    )
    # 回归保护：不应退化到 $15/$75
    assert price.get("input_per_1m") != 15.00, (
        f"openrouter/anthropic/claude-opus-4-8[1m] 不应退化到旧版 $15 定价"
    )


def test_openrouter_does_not_shadow_explicit_pricing():
    """当用户定价表已为完整代理 ID 配置条目时，该精确条目应优先于剥离后的候选。

    本测试通过 monkey-patching 临时注入一个 openrouter/... 条目，验证它被命中。
    """
    import ccs.cost as cm

    # 临时向内置定价表注入一个独立条目用于测试
    original_load = cm._load_pricing
    def patched_load():
        pricing = original_load()
        # 深拷贝以避免污染
        import copy
        pricing = copy.deepcopy(pricing)
        pricing.setdefault("providers", {}).setdefault("test_proxy", {})[
            "openrouter/claude-opus-4-8"
        ] = {
            "input_per_1m": 99.99,
            "output_per_1m": 88.88,
            "cache_read_per_1m": 7.77,
        }
        return pricing
    cm._load_pricing = patched_load
    try:
        price, _, _ = cm._resolve_price("openrouter/claude-opus-4-8", "USD", "USD")
        # 完整 ID 是第一个候选，应被精确命中
        assert price.get("input_per_1m") == 99.99, (
            f"完整 openrouter/... 条目应被精确命中（input=99.99），"
            f"实际为 {price.get('input_per_1m')}（说明被错误剥离到 claude-opus-4-8 的 $5）"
        )
    finally:
        cm._load_pricing = original_load


# ──────────────────────────────────────────────────────────────────────────────
# 任务 (f)：gpt-5.6-sol 解析（点号形式 / 连字符键 / [1m] 后缀 / 代理前缀）
# ──────────────────────────────────────────────────────────────────────────────

def test_gpt_5_6_sol_dot_form_resolves_to_short_context_pricing():
    """gpt-5.6-sol（官方点号形式）应归一化为 gpt-5-6-sol 并命中短上下文档定价。

    解析链：gpt-5.6-sol
      → 点号归一：gpt-5-6-sol（命中）→ input=$5, cached=$0.5, cache_write=$6.25, output=$30
    修复前候选只按 '-' 分段，点号形式退化到 gpt → 落入默认价（低估）。
    """
    price, price_currency, _ = _resolve("gpt-5.6-sol")
    assert price_currency == "USD", f"预期价格货币为 USD，实际为 {price_currency!r}"
    assert price.get("input_per_1m") == 5.00, (
        f"gpt-5.6-sol input 应为 $5.00，实际为 {price.get('input_per_1m')}（落入默认价 = bug）"
    )
    assert price.get("cache_read_per_1m") == 0.50, (
        f"gpt-5.6-sol cached input 应为 $0.50，实际为 {price.get('cache_read_per_1m')}"
    )
    assert price.get("cache_write_per_1m") == 6.25, (
        f"gpt-5.6-sol cache write 应为 $6.25，实际为 {price.get('cache_write_per_1m')}"
    )
    assert price.get("output_per_1m") == 30.00, (
        f"gpt-5.6-sol output 应为 $30.00，实际为 {price.get('output_per_1m')}"
    )


def test_gpt_5_6_sol_dash_form_exact_match():
    """gpt-5-6-sol（连字符形式，即价表键本身）应精确命中。"""
    price, _, _ = _resolve("gpt-5-6-sol")
    assert price.get("input_per_1m") == 5.00
    assert price.get("output_per_1m") == 30.00


def test_gpt_5_6_sol_1m_suffix_stripped():
    """gpt-5.6-sol[1m]（1M 上下文 / 缓存变体标识）应剥离后缀并命中短上下文档定价。"""
    price, _, _ = _resolve("gpt-5.6-sol[1m]")
    assert price.get("input_per_1m") == 5.00, (
        f"gpt-5.6-sol[1m] input 应为 $5.00，实际为 {price.get('input_per_1m')}"
    )
    assert price.get("output_per_1m") == 30.00, (
        f"gpt-5.6-sol[1m] output 应为 $30.00，实际为 {price.get('output_per_1m')}"
    )


def test_gpt_5_6_sol_proxy_prefix_stripped():
    """代理前缀 openrouter/gpt-5.6-sol 应剥离前缀 + 点号归一后命中定价。"""
    price, _, _ = _resolve("openrouter/gpt-5.6-sol")
    assert price.get("input_per_1m") == 5.00, (
        f"openrouter/gpt-5.6-sol input 应为 $5.00，实际为 {price.get('input_per_1m')}（可能走了默认价）"
    )
    assert price.get("output_per_1m") == 30.00, (
        f"openrouter/gpt-5.6-sol output 应为 $30.00，实际为 {price.get('output_per_1m')}"
    )


def test_gpt_5_6_sol_does_not_fallback_to_gpt_5():
    """回归保护：gpt-5.6-sol 不应退化匹配到 gpt-5（$0.625/$5）。

    候选逐段剥离链含 gpt-5-6-sol → gpt-5-6 → gpt-5 → gpt。gpt-5-6-sol
    必须先命中，否则会退化到 gpt-5 的 $0.625/$5（大幅低估）。
    """
    for model_id in ("gpt-5.6-sol", "gpt-5-6-sol", "gpt-5.6-sol[1m]"):
        price, _, _ = _resolve(model_id)
        assert price.get("input_per_1m") == 5.00, (
            f"{model_id!r} 退化匹配到了 gpt-5 的 $0.625 定价，应为 $5.00（bug 回归）"
        )
        assert price.get("output_per_1m") == 30.00, (
            f"{model_id!r} 退化匹配到了 gpt-5 的 $5 output，应为 $30.00（bug 回归）"
        )


def test_fmt_cost_multi_gpt_5_6_sol_symbol_and_magnitude():
    """fmt_cost_multi 对 gpt-5.6-sol usage 计算：无 currency 字段 → 显示 ¥（display_currency=CNY）。

    用量：input=1_000_000（$5.00）+ output=100_000（$3.00）= USD $8.00
    → CNY ¥57.44（8.00 × 7.18）。
    """
    breakdown = {
        "gpt-5.6-sol": {
            "input": 1_000_000,
            "output": 100_000,
            "cache_read": 0,
            "cache_write": 0,
        }
    }
    result = cost_mod.fmt_cost_multi(breakdown)
    assert result.startswith("¥"), (
        f"gpt-5.6-sol 无 currency 字段应按 display_currency=CNY 显示 ¥，实际为 {result!r}"
    )
    value = float(result.lstrip("¥"))
    # 正确定价 ≈ ¥57.44；若退化到 gpt-5（$0.625/$5）则约 ¥8.1。
    assert 55.0 < value < 60.0, (
        f"gpt-5.6-sol 成本预期约 ¥57.44，实际为 {result!r}"
    )
