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
    ("gpt-5.6-sol", "$2.44"),
    ("gpt-5-6-sol", "$2.44"),
    ("gpt-5.6-sol[1m]", "$2.44"),
    ("openrouter/gpt-5.6-sol", "$2.44"),
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
    ("glm-5.2", "¥3.80"),
    ("MiniMax-M3.1-Flash", "¥0.510"),
    ("mimo-v2.6-pro", "¥0.903"),
])
def test_builtin_cny_native_models(usd, model_id, expected):
    assert last_cost(model_id, **U) == expected


# ── DeepSeek 峰谷：按每次调用的时间选价 ───────────────────────────────────

@pytest.mark.parametrize("model_id, at, expected", [
    ("deepseek-v4-pro", "2026-08-15T02:00:00Z", "¥3.63"),    # 高峰 [1,4)
    ("deepseek-v4-pro", "2026-08-15T08:00:00Z", "¥3.63"),    # 高峰 [6,10)
    ("deepseek-v4-pro", "2026-08-15T05:30:00Z", "¥1.82"),    # 空闲
    ("deepseek-v4-flash", "2026-08-15T10:00:00Z", "¥0.502"), # 10 点是空闲（半开区间）
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
    # 9 + 4.5 + 1（Flash 空闲价已由 1.5 调整为 1.0）
    assert cost_mod.fmt_cost_multi({}, primary_model_id="deepseek-v4-pro", model_calls=calls) == "¥14.50"


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


# ── 第三方网关的 id 变体：前缀、后缀、版本 ────────────────────────────────

@pytest.mark.parametrize("model_id, expected", [
    ("glm-5.2", "¥3.80"),                       # 点号形式的键
    ("zai/glm-5.2", "¥3.80"),                   # 前缀剥离必须保留点号形式，否则落到目录价
    ("newapi/deepseek-v4-flash-v2", "¥1.00"),   # 前缀 + 版本后缀组合
    ("deepseek-v4-flash-newapi", "¥1.00"),      # 仅后缀
    ("openrouter/deepseek/deepseek-v4-flash", "¥1.00"),
    ("openrouter/deepseek/deepseek-chat-v3", "¥1.05"),  # 前缀 + 版本，旧链会落到默认价
    ("newapi/deepseek_v4_flash", "¥1.00"),      # 下划线拼写：仅 token 模糊匹配可达
    ("newapi/deepseek.v4.flash", "¥1.00"),      # 点号分隔：同上
    ("weihub/kimi-k2.7-code", "¥3.69"),         # 转售条目已废弃，前缀剥离后走官方价
])
def test_variant_ids_match_base_model(model_id, expected):
    assert last_cost(model_id, **U) == expected


def test_version_veto_blocks_cross_generation(usd):
    # 用户表里只有 v3 变体：v4 请求必须拒绝它，落到内置 v4 价格
    write_user_pricing(
        "providers:\n  mine:\n    myvllm-deepseek-v3-flash:\n      input_per_1m: 100\n      output_per_1m: 0\n"
    )
    assert last_cost("newapi/myvllm-deepseek-v4-flash-preview", **U) == "¥1.00"
    # 同代则命中该用户条目（前缀 + 后缀都被吃掉，仅剩版本区分）
    assert last_cost("newapi/myvllm-deepseek-v3-flash-preview", input=100_000) == "$10.00"


def test_patterns_rule_overrides_exact_chain(usd):
    write_user_pricing(
        "providers:\n  mine:\n    my-cheap-flash:\n      input_per_1m: 100\n      output_per_1m: 0\n"
        "patterns:\n"
        "  - match: \"*deepseek-v4-flash*\"\n"
        "    use: my-cheap-flash\n"
    )
    # `*` 跨 `/`，因此带网关前缀的 id 也命中；规则优先于精确链
    assert last_cost("newapi/deepseek-v4-flash-v2", input=100_000) == "$10.00"
    assert last_cost("deepseek-v4-flash", input=100_000) == "$10.00"
    assert last_cost("deepseek-v4-pro", input=100_000) == "¥0.900"  # 未命中规则照常走表


# ── 币种规则：有人民币价就按人民币算，没有才用美元价 ──────────────────────

@pytest.mark.parametrize("entry_yaml, expected", [
    # 双币种：CNY 块优先，显示 ¥（USD 写在前面也一样）
    ("      prices:\n        USD:\n          input_per_1m: 10\n          output_per_1m: 0\n"
     "        CNY:\n          input_per_1m: 100\n          output_per_1m: 0\n", "¥10.00"),
    # 块顺序无关
    ("      prices:\n        CNY:\n          input_per_1m: 50\n          output_per_1m: 0\n"
     "        USD:\n          input_per_1m: 10\n          output_per_1m: 0\n", "¥5.00"),
    # 只有美元价：回退美元价，再按汇率显示为 ¥
    ("      prices:\n        USD:\n          input_per_1m: 10\n          output_per_1m: 0\n", "¥7.18"),
])
def test_cny_price_preferred_over_usd(entry_yaml, expected):
    write_user_pricing("providers:\n  mine:\n    my-model-x:\n" + entry_yaml)
    assert last_cost("my-model-x", input=100_000) == expected


def test_usd_price_kept_when_display_is_usd(usd):
    write_user_pricing(
        "providers:\n  mine:\n    my-model-y:\n      prices:\n"
        "        USD:\n          input_per_1m: 10\n          output_per_1m: 0\n"
    )
    assert last_cost("my-model-y", input=100_000) == "$1.00"


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
