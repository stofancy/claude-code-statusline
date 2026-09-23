"""计价规则的表驱动测试，只通过公开函数 fmt_last_cost / fmt_cost_multi。

每行 = 一次调用的用量 → 期望的费用字符串，同时锁定「模型 id 解析到哪条价格」
与「价格数值」。默认用量 U：输入、输出、缓存读取各 10 万 token（prompt 20 万，
不越过 200k 分档阈值）。

Run: python -m pytest tests/test_pricing.py -v
"""

import pytest

import ccs.catalog as catalog_mod
import ccs.cost as cost_mod

U = dict(input=100_000, output=100_000, cache_read=100_000, cache_write=0)


def last_cost(model_id, input=0, output=0, cache_read=0, cache_write=0):
    return cost_mod.fmt_last_cost(model_id, input, output, cache_read, cache_write)


def call_cost(model_id, at, **usage):
    u = {**dict(input=0, output=0, cache_read=0, cache_write=0), **usage}
    return cost_mod.fmt_cost_multi({}, model_calls={model_id: [(at, u)]})


@pytest.fixture
def usd(monkeypatch):
    monkeypatch.setenv("CCS_CURRENCY", "USD")


def install_catalog(models=None, rates=None):
    if models is not None:
        catalog_mod._write_json(catalog_mod._MODELS_PATH, {"models": models})
    if rates is not None:
        catalog_mod._write_json(catalog_mod._FX_PATH, {"rates": rates})


def write_user_pricing(text):
    cost_mod._USER_PRICING.parent.mkdir(parents=True, exist_ok=True)
    cost_mod._USER_PRICING.write_text(text, encoding="utf-8")


# ── 内置表：模型 id 解析 + 价格 ───────────────────────────────────────────

@pytest.mark.parametrize("model_id, expected", [
    # Opus 4.5+ 为 $5/$25/$0.5；不得退化到 claude-opus-4 的 $15/$75
    ("claude-opus-5", "$3.05"),
    ("claude-opus-4-8", "$3.05"),
    ("claude-opus-4-8[1m]", "$3.05"),
    ("claude-opus-4-8[1M]", "$3.05"),
    ("claude-opus-4.8", "$3.05"),                        # transcript 的点号版本
    ("openrouter/anthropic/claude-opus-4-8", "$3.05"),   # 代理前缀
    ("openrouter/anthropic/claude-opus-4-8[1m]", "$3.05"),
    ("claude-opus-4", "$9.15"),                          # 旧 Opus 仍为 $15/$75
    ("claude-haiku-4.5", "$0.610"),
    ("claude-sonnet-5", "$1.22"),                         # $2/$10 已转为标准价
    ("opencode/claude-sonnet-4-6", "$1.83"),
    ("gpt-5.6-sol", "$3.55"),
    ("gpt-5-6-sol", "$3.55"),
    ("gpt-5.6-sol[1m]", "$3.55"),
    ("openrouter/gpt-5.6-sol", "$3.55"),
    ("grok-4.6", "$0.850"),
    ("grok-4.5", "$0.830"),
    ("grok-4.5-build", "$0.830"),                        # 尾段剥离回到点号键
])
def test_builtin_usd_models(usd, model_id, expected):
    assert last_cost(model_id, **U) == expected


def test_anthropic_cache_write_billed(usd):
    assert last_cost("claude-opus-4-8", cache_write=100_000) == "$0.625"


def test_anthropic_1h_cache_write_is_2x_input(usd):
    # 1 小时缓存写入 = 2× 基础输入价（$5 → $10）；只有标记为 1h 的部分按此计
    assert cost_mod.fmt_last_cost("claude-opus-4-8", 0, 0, 0, 100_000, 100_000) == "$1.00"
    assert cost_mod.fmt_last_cost("claude-opus-4-8", 0, 0, 0, 100_000, 40_000) == "$0.775"


@pytest.mark.parametrize("model_id, expected", [
    # 人民币原生价：无论显示币种，都以 ¥ 显示原价
    ("weihub/glm-5.2", "¥3.76"),
    ("weihub/kimi-k2.7-code", "¥3.48"),
])
def test_builtin_cny_native_models(usd, model_id, expected):
    assert last_cost(model_id, **U) == expected


# ── DeepSeek 峰谷：按每次调用的时间选价 ───────────────────────────────────

@pytest.mark.parametrize("model_id, at, expected", [
    ("deepseek-v4-pro", "2026-08-15T02:00:00Z", "¥3.63"),    # 高峰 [1,4)
    ("deepseek-v4-pro", "2026-08-15T08:00:00Z", "¥3.63"),    # 高峰 [6,10)
    ("deepseek-v4-pro", "2026-08-15T05:30:00Z", "¥1.82"),    # 空闲
    ("deepseek-v4-flash", "2026-08-15T10:00:00Z", "¥0.605"), # 10 点是空闲（半开区间）
    ("openrouter/deepseek/deepseek-v4-pro", "2026-08-15T05:30:00Z", "¥1.82"),
    ("openrouter/deepseek-v4-pro", "2026-08-15T05:30:00Z", "¥1.82"),
    ("opencode-go/deepseek-v4-pro", "2026-08-15T05:30:00Z", "¥1.82"),
])
def test_tide_pricing(model_id, at, expected):
    assert call_cost(model_id, at, **U) == expected


def test_tide_mixed_calls_priced_individually():
    one_m = dict(input=1_000_000, output=0, cache_read=0, cache_write=0)
    calls = {
        "deepseek-v4-pro": [("2026-08-15T02:00:00Z", one_m), ("2026-08-15T05:30:00Z", one_m)],
        "deepseek-v4-flash": [("2026-08-15T05:30:00Z", one_m)],
    }
    # 9 + 4.5 + 1.5
    assert cost_mod.fmt_cost_multi({}, primary_model_id="deepseek-v4-pro", model_calls=calls) == "¥15.00"


# ── 上下文长度分档 ────────────────────────────────────────────────────────

def test_tier_selected_per_call(usd):
    small = dict(input=100_000, output=0, cache_read=0, cache_write=0)
    big = dict(input=300_000, output=0, cache_read=0, cache_write=0)
    # 0.1M × $2 + 0.3M × $4
    out = cost_mod.fmt_cost_multi({}, model_calls={"grok-4.6": [("", small), ("", big)]})
    assert out == "$1.40"


def test_tier_counts_cached_prompt(usd):
    # prompt = 15 万输入 + 10 万缓存读取 = 25 万 > 20 万 → $4 / $1
    assert last_cost("grok-4.6", input=150_000, cache_read=100_000) == "$0.700"


# ── models.dev 目录与内置表的优先级 ───────────────────────────────────────

CATALOG = {
    "claude-opus-5-5": {"input_per_1m": 4.0, "output_per_1m": 20.0, "cache_read_per_1m": 0.2},
    "claude-opus-5": {"input_per_1m": 3.0, "output_per_1m": 15.0, "cache_read_per_1m": 0.3},
    "minimax-m3": {"input_per_1m": 0.3, "output_per_1m": 1.2},
}


@pytest.mark.parametrize("model_id, expected", [
    ("claude-opus-5-5[1m]", "$2.42"),  # 目录里的新模型，不再被剥离成 claude-opus-5
    ("claude-opus-5", "$1.83"),        # 同名时目录覆盖内置普通条目
])
def test_catalog_price_wins(usd, model_id, expected):
    install_catalog(CATALOG)
    assert last_cost(model_id, **U) == expected


def test_without_catalog_new_model_falls_back_to_builtin_prefix(usd):
    assert last_cost("claude-opus-5-5", **U) == "$3.05"


def test_catalog_does_not_override_cny_native_entry(usd):
    install_catalog(CATALOG)
    assert last_cost("MiniMax-M3", **U).startswith("¥")


def test_pricing_api_disabled_ignores_catalog(usd, monkeypatch):
    install_catalog(CATALOG)
    monkeypatch.setenv("CCS_PRICING_API", "0")
    assert last_cost("claude-opus-5-5", **U) == "$3.05"


# ── 用户 pricing.yaml 与汇率 ──────────────────────────────────────────────

def test_user_entry_beats_catalog_and_builtin_stays_available(usd):
    install_catalog(CATALOG)
    write_user_pricing("providers:\n  mine:\n    claude-opus-5-5:\n      input_per_1m: 10\n      output_per_1m: 10\n")
    assert last_cost("claude-opus-5-5", input=100_000) == "$1.00"
    assert last_cost("claude-opus-4-8", **U) == "$3.05"  # 用户文件没写的模型仍走内置表


def test_user_entry_shadows_proxy_prefix_match(usd):
    write_user_pricing("providers:\n  mine:\n    openrouter/claude-opus-4-8:\n      input_per_1m: 99.99\n      output_per_1m: 1\n")
    assert last_cost("openrouter/claude-opus-4-8", input=100_000) == "$10.00"


@pytest.mark.parametrize("fetched, user_pin, expected", [
    (None, None, "¥21.90"),   # 内置快照 7.18
    (6.5, None, "¥19.82"),    # 每日拉取的汇率
    (6.5, 7.0, "¥21.35"),     # 用户 pricing.yaml 钉住的汇率优先
])
def test_fx_precedence(fetched, user_pin, expected):
    if fetched is not None:
        install_catalog(rates={"USD": 1.0, "CNY": fetched})
    if user_pin is not None:
        write_user_pricing(f"fx_rates:\n  CNY: {user_pin}\n")
    assert last_cost("claude-opus-4-8", **U) == expected
