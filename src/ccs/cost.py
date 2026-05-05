"""Multi-provider cost calculation with configurable pricing table.

All cumulative cost goes through fmt_cost_multi, which uses actual per-model
token breakdowns from model_usage (not ratio-based estimation).
"""

from pathlib import Path

import yaml

_BUILTIN_PRICING = Path(__file__).parent / "pricing.yaml"
_USER_PRICING = Path.home() / ".claude" / "statusline" / "pricing.yaml"
_DEFAULT_PRICING = {"input_per_1m": 1.0, "output_per_1m": 4.0, "cache_read_per_1m": 0.1}

_pricing_cache: dict | None = None


def _load_pricing() -> dict:
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    for p in (_USER_PRICING, _BUILTIN_PRICING):
        if p.exists():
            with open(p) as f:
                _pricing_cache = yaml.safe_load(f) or {}
                return _pricing_cache
    _pricing_cache = {}
    return _pricing_cache


def _find_model_price(model_id: str, pricing: dict) -> dict:
    providers = pricing.get("providers", {})
    for provider_cfg in providers.values():
        if isinstance(provider_cfg, dict):
            if model_id in provider_cfg:
                return provider_cfg[model_id]
            models = provider_cfg.get("models", {})
            if isinstance(models, dict) and model_id in models:
                return models[model_id]
    if model_id in pricing:
        return pricing[model_id]
    return {}


def _resolve_price(model_id: str) -> dict:
    pricing = _load_pricing()
    mp = _find_model_price(model_id, pricing)
    if mp:
        return mp
    stripped = model_id.replace("[1m]", "").rstrip("-0123456789.")
    mp = _find_model_price(stripped, pricing)
    return mp or _DEFAULT_PRICING


def _resolve(model_id: str) -> tuple[dict, str, str]:
    """Returns (price_dict, currency, symbol) — called once per tick."""
    mp = _resolve_price(model_id)
    currency = _load_pricing().get("default_currency", "CNY")
    symbol = {"CNY": "¥", "USD": "$", "EUR": "€"}.get(currency, currency)
    return mp, currency, symbol


def _per_1m(price: float, tokens: int) -> float:
    return (tokens / 1_000_000.0) * price


def _fmt_cost_val(symbol: str, cost: float) -> str:
    if cost < 0.01:
        return f"{symbol}{cost:.4f}"
    elif cost < 1.0:
        return f"{symbol}{cost:.3f}"
    elif cost < 100.0:
        return f"{symbol}{cost:.2f}"
    else:
        return f"{symbol}{cost:.1f}"


def fmt_cost_multi(model_breakdown: dict) -> str:
    """按模型分别定价后加总，返回精确的混合模型累积成本。

    *model_breakdown*: {model_id: {input, output, cache_read, cache_write}, ...}
    """
    if not model_breakdown:
        return "-"

    _, _, symbol = _resolve(next(iter(model_breakdown)) if model_breakdown else "unknown")
    total_cost = 0.0

    for model_id, usage in model_breakdown.items():
        mp, _, _ = _resolve(model_id)
        input_price = mp.get("input_per_1m", mp.get("input", _DEFAULT_PRICING["input_per_1m"]))
        output_price = mp.get("output_per_1m", mp.get("output", _DEFAULT_PRICING["output_per_1m"]))
        cache_hit_price = mp.get("input_cache_hit_per_1m", mp.get("cache_read_per_1m", input_price * 0.1))

        raw_input = usage.get("input", 0)
        total_output = usage.get("output", 0)
        cache_read = usage.get("cache_read", 0)
        cache_write = usage.get("cache_write", 0)

        # raw_input = pc_input = 总输入（含 cache_read，对 DeepSeek 也含 cache_write）
        # non_cache_input = 未命中缓存的输入（不含 cache_read 的纯 input）
        non_cache_input = max(0, raw_input - cache_read)

        total_cost += (_per_1m(input_price, non_cache_input) +
                       _per_1m(cache_hit_price, cache_read) +
                       _per_1m(output_price, total_output))

        # Anthropic: cache_write 是 input_tokens 之外的额外 token，需单独计费
        if "cache_write_per_1m" in mp:
            total_cost += _per_1m(mp["cache_write_per_1m"], cache_write)

    return _fmt_cost_val(symbol, total_cost)


def fmt_last_cost(
    model_id: str,
    per_call_input: int,
    per_call_output: int,
    per_call_cache_read: int,
) -> str:
    """Cost of the most recent turn using exact per-call values."""
    mp, _, symbol = _resolve(model_id)
    input_price = mp.get("input_per_1m", mp.get("input", 1.0))
    output_price = mp.get("output_per_1m", mp.get("output", 4.0))
    cache_hit_price = mp.get("input_cache_hit_per_1m", mp.get("cache_read_per_1m", input_price * 0.1))

    cost = (_per_1m(input_price, per_call_input) +
            _per_1m(cache_hit_price, per_call_cache_read) +
            _per_1m(output_price, per_call_output))

    if cost < 0.0001:
        return f"{symbol}0"
    return _fmt_cost_val(symbol, cost)
