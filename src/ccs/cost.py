"""Multi-provider cost calculation with configurable pricing table.

All cumulative cost goes through fmt_cost_multi, which uses actual per-model
token breakdowns from model_usage (not ratio-based estimation).

Currency model
--------------
The pricing table is denominated in *one* canonical currency (declared by the
top-level ``base_currency`` key — defaults to ``USD``). At display time the
user-selected currency (``display_currency`` or env ``CCS_CURRENCY``) is
applied through the ``fx_rates`` block. This decouples the source-of-truth
prices from how they are presented and lets non-CNY users avoid rewriting the
whole table.
"""

import os
from pathlib import Path

import yaml

_BUILTIN_PRICING = Path(__file__).parent / "pricing.yaml"
_USER_PRICING = Path.home() / ".claude" / "statusline" / "pricing.yaml"
_DEFAULT_PRICING = {"input_per_1m": 1.0, "output_per_1m": 4.0, "cache_read_per_1m": 0.1}

_CURRENCY_SYMBOLS = {
    "USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£", "JPY": "¥",
    "AUD": "A$", "INR": "₹", "HKD": "HK$", "SGD": "S$", "KRW": "₩",
    "CAD": "C$", "CHF": "CHF", "TWD": "NT$",
}

_DEBUG_LOG = Path.home() / ".claude" / "statusline" / "debug.log"

_pricing_cache: dict | None = None
_pricing_mtime: float = 0.0
_pricing_source: Path | None = None
_unresolved_logged: set[str] = set()


def _load_pricing() -> dict:
    """Load pricing YAML. Re-reads on mtime change so edits take effect without restart."""
    global _pricing_cache, _pricing_mtime, _pricing_source

    target: Path | None = None
    for p in (_USER_PRICING, _BUILTIN_PRICING):
        if p.exists():
            target = p
            break

    if target is None:
        _pricing_cache = {}
        return _pricing_cache

    try:
        mtime = target.stat().st_mtime
    except OSError:
        mtime = 0.0

    if (_pricing_cache is not None
            and _pricing_source == target
            and _pricing_mtime == mtime):
        return _pricing_cache

    try:
        with open(target, "r", encoding="utf-8") as f:
            _pricing_cache = yaml.safe_load(f) or {}
    except OSError:
        _pricing_cache = {}
    _pricing_mtime = mtime
    _pricing_source = target
    return _pricing_cache


def _debug_unresolved(model_id: str) -> None:
    """Log unresolved model IDs once per process when CCS_DEBUG=1."""
    if os.getenv("CCS_DEBUG") != "1":
        return
    if model_id in _unresolved_logged:
        return
    _unresolved_logged.add(model_id)
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[cost] unresolved model id, using default pricing: {model_id}\n")
    except OSError:
        pass


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

    stripped = model_id.replace("[1m]", "")
    if stripped != model_id:
        mp = _find_model_price(stripped, pricing)
        if mp:
            return mp

    parts = stripped.split("-")
    while len(parts) > 1:
        parts.pop()
        mp = _find_model_price("-".join(parts), pricing)
        if mp:
            return mp

    _debug_unresolved(model_id)
    return _DEFAULT_PRICING


def _currency_settings() -> tuple[str, str, float, str]:
    """Return (base_currency, display_currency, fx_multiplier, symbol).

    Selection order for display currency:
      1. CCS_CURRENCY env var
      2. top-level ``display_currency`` in YAML
      3. legacy top-level ``default_currency`` (preserves old configs)
      4. base_currency itself

    Symbol falls back to the currency code if unmapped.
    """
    pricing = _load_pricing()
    base = (pricing.get("base_currency") or "USD").upper()

    display = (os.getenv("CCS_CURRENCY")
               or pricing.get("display_currency")
               or pricing.get("default_currency")
               or base).upper()

    fx_rates = pricing.get("fx_rates") or {}
    base_rate = float(fx_rates.get(base, 1.0))
    target_rate = float(fx_rates.get(display, 1.0))
    if base_rate <= 0:
        base_rate = 1.0
    # multiplier converts a value in `base` into `display`
    multiplier = target_rate / base_rate

    symbol = _CURRENCY_SYMBOLS.get(display, display + " ")
    return base, display, multiplier, symbol


def _resolve(model_id: str) -> tuple[dict, str, float, str]:
    """Returns (price_dict, display_currency, fx_multiplier, symbol)."""
    mp = _resolve_price(model_id)
    _, display, mult, symbol = _currency_settings()
    return mp, display, mult, symbol


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


def _model_cost_in_base(mp: dict, usage: dict) -> float:
    """Compute one model's cost in the *base* currency (pre-FX)."""
    input_price = mp.get("input_per_1m", mp.get("input", _DEFAULT_PRICING["input_per_1m"]))
    output_price = mp.get("output_per_1m", mp.get("output", _DEFAULT_PRICING["output_per_1m"]))
    cache_hit_price = mp.get("input_cache_hit_per_1m",
                             mp.get("cache_read_per_1m", input_price * 0.1))

    non_cache_input = usage.get("input", 0)
    total_output = usage.get("output", 0)
    cache_read = usage.get("cache_read", 0)
    cache_write = usage.get("cache_write", 0)

    cost = (_per_1m(input_price, non_cache_input)
            + _per_1m(cache_hit_price, cache_read)
            + _per_1m(output_price, total_output))

    if "cache_write_per_1m" in mp:
        cost += _per_1m(mp["cache_write_per_1m"], cache_write)

    return cost


def fmt_cost_multi(model_breakdown: dict) -> str:
    """Sum per-model costs in the base currency, then apply FX to display."""
    if not model_breakdown:
        return "-"

    _, _, multiplier, symbol = _currency_settings()
    total = 0.0
    for model_id, usage in model_breakdown.items():
        total += _model_cost_in_base(_resolve_price(model_id), usage)
    return _fmt_cost_val(symbol, total * multiplier)


def fmt_last_cost(
    model_id: str,
    per_call_input: int,
    per_call_output: int,
    per_call_cache_read: int,
    per_call_cache_write: int = 0,
) -> str:
    """Cost of the most recent turn using exact per-call values."""
    mp = _resolve_price(model_id)
    _, _, multiplier, symbol = _currency_settings()
    cost_base = _model_cost_in_base(mp, {
        "input": per_call_input,
        "output": per_call_output,
        "cache_read": per_call_cache_read,
        "cache_write": per_call_cache_write,
    })
    cost = cost_base * multiplier
    if cost < 0.0001:
        return f"{symbol}0"
    return _fmt_cost_val(symbol, cost)
