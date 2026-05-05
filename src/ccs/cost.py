"""Multi-provider cost calculation with configurable pricing table.

total_input_tokens includes cache reads (per Anthropic API spec).
We subtract estimated cache before billing at full input rate.
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


def fmt_cost(
    model_id: str,
    total_input_tokens: int,
    total_output_tokens: int,
    per_call_cache_read: int = 0,
    per_call_input: int = 0,
) -> str:
    """Session cumulative cost. Uses latest call's cache ratio for estimation."""
    mp, _, symbol = _resolve(model_id)
    input_price = mp.get("input_per_1m", mp.get("input", 1.0))
    output_price = mp.get("output_per_1m", mp.get("output", 4.0))
    cache_hit_price = mp.get("input_cache_hit_per_1m", mp.get("cache_read_per_1m", input_price * 0.1))

    per_call_total = per_call_cache_read + per_call_input
    if per_call_total > 0 and per_call_cache_read > 0:
        ratio = per_call_cache_read / per_call_total
        est_cached = int(total_input_tokens * ratio)
        est_non_cached = total_input_tokens - est_cached
        cost = (_per_1m(input_price, est_non_cached) +
                _per_1m(cache_hit_price, est_cached) +
                _per_1m(output_price, total_output_tokens))
    else:
        cost = (_per_1m(input_price, total_input_tokens) +
                _per_1m(output_price, total_output_tokens))
    return _fmt_cost_val(symbol, cost)


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

        total_input = usage.get("input", 0) + usage.get("cache_read", 0) + usage.get("cache_write", 0)
        total_output = usage.get("output", 0)
        cache_read = usage.get("cache_read", 0)

        if total_input > 0 and cache_read > 0:
            ratio = cache_read / total_input
            est_cached = int(total_input * ratio)
            est_non_cached = total_input - est_cached
            total_cost += (_per_1m(input_price, est_non_cached) +
                           _per_1m(cache_hit_price, est_cached) +
                           _per_1m(output_price, total_output))
        else:
            total_cost += (_per_1m(input_price, total_input) +
                           _per_1m(output_price, total_output))

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
