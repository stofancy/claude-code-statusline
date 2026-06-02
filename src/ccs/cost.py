"""Multi-provider cost calculation with configurable pricing table.

All cumulative cost goes through :func:`fmt_cost_multi`, which uses actual
per-model token breakdowns from ``model_usage`` (not ratio-based estimation).

Currency model
--------------
Every model has a *native* currency — the currency in which its prices are
authored. A model can declare it explicitly via ``currency: <CODE>``, or
implicitly (defaults to ``base_currency`` when omitted).

A model may also publish prices in multiple currencies via the ``prices``
field, keyed by ISO 4217 code::

    minimax:
      MiniMax-M3:
        currency: CNY
        prices:
          CNY: { input_per_1m: 2.10, output_per_1m: 8.40, ... }
          USD: { input_per_1m: 0.29, output_per_1m: 1.17, ... }

Resolution order for the *active* price block of a model:

  1. ``prices[model_currency]`` — the model's own native block. The cost
     is exact, no FX. Preferred path whenever the model publishes a
     block in its declared currency.
  2. ``prices[display_currency]`` — exact value in the user's chosen
     currency, no FX. Used when the model has no native block.
  3. ``prices[base_currency]`` — exact value in the FX pivot, then
     FX-converted to the model's target display currency.
  4. First available key in ``prices`` — last-resort fallback.
  5. Flat dict (legacy) — used as-is; price currency = ``currency`` field
     if present, else ``base_currency``.

The model's **target display currency** (the symbol the user sees) is the
declared ``currency`` field if set, else the user's ``display_currency``.
This is computed separately from the price currency and joined by a
single FX step (when they differ) so the displayed number is always in
the user's intended currency while the underlying math stays exact.

Why split display from price currency?

  • Models with multi-currency pricing stay precise: we never FX-round
    a published list price (CNY 2.10 stays 2.10, not 2.10 / 7.18 × 7.18).
  • A model whose list price is denominated in CNY displays as ¥ even
    when the user has ``display_currency: USD`` — the user reads the
    native price they actually pay, not a 7.18-multiplied shadow.
  • When a model declares ``currency: CNY`` but only ships ``prices:
    { USD: ... }``, the cost is computed in USD (authoritative) and
    FX-converted to CNY for display — the user still sees ¥, without
    us inventing a CNY price that doesn't exist.
  • When the breakdown mixes multiple target currencies, the totals are
    FX-converted into ``display_currency`` for the final sum.
"""

import os
import re
from pathlib import Path

import yaml

# 剥离模型 ID 末尾的 ``[1m]`` / ``[1M]`` 后缀。该正则覆盖两类来源：
#
#   1. MiniMax / DeepSeek 等提供商的「1 小时缓存变体」标识——当请求携带
#      1-hour cache hint 时，这些 API 会在模型 ID 后追加 ``[1m]``（偶尔
#      追加两次 ``[1M][1m]``）。原始设计即为此场景。
#
#   2. Anthropic 的「1M 上下文窗口」标识——Claude Code 对 1M context 变体
#      的模型 ID 追加相同形态的 ``[1m]`` 后缀（如 ``claude-opus-4-8[1m]``）。
#      字面形式与上述缓存标识完全相同，因此同一正则可一并处理，剥离后得到
#      精确的定价键名（``claude-opus-4-8``），防止逐段退化匹配到旧版模型
#      （如 ``claude-opus-4``，其定价为 $15/$75，约为正确值的 3 倍）。
#
# 忽略大小写是因为实测中见过 ``[1m]`` 和 ``[1M]`` 两种写法。
# 处理示例：
#   MiniMax-M3                 → MiniMax-M3         （无后缀，不变）
#   MiniMax-M3[1m]             → MiniMax-M3         （缓存变体）
#   MiniMax-M3[1M][1m]         → MiniMax-M3         （双重缓存变体）
#   claude-opus-4-8[1m]        → claude-opus-4-8    （1M 上下文窗口变体）
#   claude-opus-4-8[1M]        → claude-opus-4-8    （大写，同上）
_CACHE_SUFFIX_RE = re.compile(r"\[1m\]", re.IGNORECASE)

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


def _find_model_entry(model_id: str, pricing: dict) -> dict | None:
    """Return the model block (dict) for ``model_id`` or ``None``.

    Search order: provider namespaces → top-level → nested ``models``
    maps. If the exact id is not found, a **case-insensitive** scan
    is used as a fallback so variants like ``minimax-m3``,
    ``MINIMAX-M3`` or ``MiniMaxM3`` still resolve to ``MiniMax-M3``.
    """
    providers = pricing.get("providers", {})

    # 1. Exact match (preserves priority for the verbatim id).
    for provider_cfg in providers.values():
        if not isinstance(provider_cfg, dict):
            continue
        if model_id in provider_cfg:
            return provider_cfg[model_id]
        models = provider_cfg.get("models", {})
        if isinstance(models, dict) and model_id in models:
            return models[model_id]
    if model_id in pricing:
        return pricing[model_id]

    # 2. Case-insensitive fallback.
    target = model_id.casefold()
    for provider_cfg in providers.values():
        if not isinstance(provider_cfg, dict):
            continue
        for k, v in provider_cfg.items():
            if k == "models" or not isinstance(v, dict):
                continue
            if k.casefold() == target:
                return v
        models = provider_cfg.get("models", {})
        if isinstance(models, dict):
            for k, v in models.items():
                if k.casefold() == target:
                    return v
    for k, v in pricing.items():
        if isinstance(v, dict) and k.casefold() == target:
            return v
    return None


def _resolve_price(
    model_id: str,
    display_currency: str,
    base_currency: str,
) -> tuple[dict, str, str]:
    """Return ``(price_dict, price_currency, target_display_currency)``.

    The three return values:

    * ``price_dict``        — the resolved price block (after the
      ``prices``-map selection rules below).
    * ``price_currency``    — the ISO code of the currency the price is
      actually denominated in. May differ from the model's declared
      ``currency`` if that currency's block is missing from ``prices``
      and we fell back to another block.
    * ``target_display_currency`` — the currency to render this model's
      cost in:

      - The model's declared ``currency`` field, **if present** — this is
        the explicit "I am a CNY-native model" signal. Per-model native
        display is the right answer for that model.
      - ``display_currency`` otherwise — the user has no per-model
        preference signal, so the cost is rendered in whatever display
        currency they configured.

    The split between ``price_currency`` and ``target_display_currency``
    matters when a model declares ``currency: CNY`` but only ships a
    ``prices:`` block in ``USD``: the cost is computed in USD (the only
    authoritative block) and FX-converted into CNY for display, so the
    user still sees ¥ without us inventing a fake CNY price.
    """
    pricing = _load_pricing()
    entry = _find_model_entry(model_id, pricing)

    # Strip provider-specific cache-variant suffixes like ``[1m]``,
    # ``[1M]``, or even doubled ``[1M][1m]`` (the suffix is appended
    # case-insensitively, and some APIs append it twice when the model
    # is invoked with a 1-hour cache hint on top of the default 5-min).
    # Then progressively shorter prefixes until a match is found.
    candidates: list[str] = [model_id]
    stripped = _CACHE_SUFFIX_RE.sub("", model_id)
    if stripped != model_id:
        candidates.append(stripped)
    parts = stripped.split("-")
    while len(parts) > 1:
        parts.pop()
        candidates.append("-".join(parts))

    for cand in candidates:
        e = _find_model_entry(cand, pricing)
        if e is not None:
            entry = e
            break

    if entry is None:
        _debug_unresolved(model_id)
        return _DEFAULT_PRICING, base_currency, base_currency

    has_explicit_currency = "currency" in entry
    model_currency = (entry.get("currency") or base_currency).upper()
    price, price_currency = _select_price_block(
        entry, model_currency, display_currency, base_currency
    )
    target = model_currency if has_explicit_currency else display_currency
    return price, price_currency, target


def _select_price_block(
    entry: dict,
    model_currency: str,
    display_currency: str,
    base_currency: str,
) -> tuple[dict, str]:
    """Pick the price block whose currency best matches the user's intent.

    Multi-currency ``prices`` map (resolution order):

      1. ``prices[model_currency]`` — exact value in the model's native
         currency, no FX. Preferred when the model publishes a block in
         its declared currency.
      2. ``prices[display_currency]`` — exact value in the user's chosen
         display currency, no FX. Used when the model doesn't ship a
         native block.
      3. ``prices[base_currency]`` — exact value in the FX pivot, then
         FX-converted to the model's target display currency.
      4. First available key — last-resort fallback.

    Legacy flat dict (no ``prices`` key):

      • Use as-is. Returned price currency is the ``model_currency``
        argument (i.e. ``entry['currency']`` if declared, else
        ``base_currency``).
    """
    prices_map = entry.get("prices")
    if isinstance(prices_map, dict) and prices_map:
        if (model_currency in prices_map
                and isinstance(prices_map[model_currency], dict)):
            return prices_map[model_currency], model_currency
        if (display_currency in prices_map
                and isinstance(prices_map[display_currency], dict)):
            return prices_map[display_currency], display_currency
        if (base_currency in prices_map
                and isinstance(prices_map[base_currency], dict)):
            return prices_map[base_currency], base_currency
        for cur, block in prices_map.items():
            if isinstance(block, dict):
                return block, cur
        # Malformed prices map — fall through to flat treatment.

    return entry, model_currency


def _currency_settings() -> tuple[str, str, dict]:
    """Return ``(base_currency, display_currency, fx_rates)``.

    Display currency precedence (highest first):
      1. ``CCS_CURRENCY`` env var
      2. ``display_currency`` in YAML
      3. ``default_currency`` in YAML (legacy alias)
      4. ``base_currency`` itself
    """
    pricing = _load_pricing()
    base = (pricing.get("base_currency") or "USD").upper()

    display = (os.getenv("CCS_CURRENCY")
               or pricing.get("display_currency")
               or pricing.get("default_currency")
               or base).upper()

    fx_rates = pricing.get("fx_rates") or {}
    return base, display, fx_rates


def _symbol_for(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get(currency, currency + " ")


def _fx_convert(amount: float, from_cur: str, to_cur: str, fx_rates: dict) -> float:
    """Convert ``amount`` from ``from_cur`` to ``to_cur`` using ``fx_rates``.

    Rates express *units of currency per 1 USD* (USD is implicitly 1.0).
    So 1 CNY = (1 / 7.18) USD = (USD_rate / CNY_rate) in display currency
    when target is USD, or analogously for any pair.
    """
    if from_cur == to_cur:
        return amount
    from_rate = float(fx_rates.get(from_cur, 1.0))
    to_rate = float(fx_rates.get(to_cur, 1.0))
    if from_rate <= 0:
        from_rate = 1.0
    return amount * (to_rate / from_rate)


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


def _model_cost_in_native(price: dict, usage: dict) -> float:
    """Compute one model's cost in its *native* currency (no FX)."""
    input_price = price.get("input_per_1m", price.get("input", _DEFAULT_PRICING["input_per_1m"]))
    output_price = price.get("output_per_1m", price.get("output", _DEFAULT_PRICING["output_per_1m"]))
    cache_hit_price = price.get("input_cache_hit_per_1m",
                                price.get("cache_read_per_1m", input_price * 0.1))

    non_cache_input = usage.get("input", 0)
    total_output = usage.get("output", 0)
    cache_read = usage.get("cache_read", 0)
    cache_write = usage.get("cache_write", 0)

    cost = (_per_1m(input_price, non_cache_input)
            + _per_1m(cache_hit_price, cache_read)
            + _per_1m(output_price, total_output))

    if "cache_write_per_1m" in price:
        cost += _per_1m(price["cache_write_per_1m"], cache_write)

    return cost


def fmt_cost_multi(
    model_breakdown: dict,
    primary_model_id: str | None = None,
) -> str:
    """Render the cumulative cost string for a per-model usage breakdown.

    Two independent grouping dimensions are kept separate:

    * **Price currency** (from the resolved ``prices`` block) drives the
      unit math — we don't invent prices in currencies the model doesn't
      publish. If the chosen block is in USD but display wants CNY, we
      apply FX rather than fabricate a number.
    * **Target display currency** (the model's declared ``currency`` if
      set, else the user's ``display_currency``) drives the *output*
      symbol. This is the currency the user actually sees.

    The optional ``primary_model_id`` selects which target currency wins
    when the breakdown contains models with different targets. Pass the
    model id from the statusline's stdin JSON — that is the model the
    user is *currently* chatting with, so its declared currency is the
    currency the user thinks in. Subagents using a different currency
    are FX-converted into the primary's currency, not the other way
    around. This keeps ``TOTAL`` and ``LAST`` consistent on the same
    line: both reflect the primary model's native price.

    When ``primary_model_id`` is omitted, the breakdown is aggregated
    into the user's ``display_currency`` (the legacy behavior).
    """
    if not model_breakdown:
        return "-"

    base, display, fx_rates = _currency_settings()

    # Sum per model: compute cost in price_currency, then FX-convert to
    # the model's target display currency. Group by target so we can tell
    # whether aggregation can be done in one currency or must FX-mix.
    by_target: dict[str, float] = {}
    for model_id, usage in model_breakdown.items():
        price, price_currency, target = _resolve_price(model_id, display, base)
        cost = _model_cost_in_native(price, usage)
        if price_currency != target:
            cost = _fx_convert(cost, price_currency, target, fx_rates)
        by_target[target] = by_target.get(target, 0.0) + cost

    if not by_target:
        return "-"

    # Single target: the breakdown sums to one currency already. Render
    # directly in that currency — no FX, no need for the caller to
    # identify a primary model. This is the path that makes
    # ``fmt_cost_multi({"MiniMax-M3": ...})`` show ¥ even without
    # ``primary_model_id``.
    if len(by_target) == 1:
        target, total = next(iter(by_target.items()))
        return _fmt_cost_val(_symbol_for(target), total)

    # Mixed targets: the caller must tell us which target wins, otherwise
    # we fall back to the user's ``display_currency``. With
    # ``primary_model_id`` set, that model's target becomes the
    # aggregation currency — keeping ``TOTAL`` and ``LAST`` consistent
    # on the same line when subagents use a different currency.
    if primary_model_id:
        _, _, display_target = _resolve_price(primary_model_id, display, base)
    else:
        display_target = display

    total = 0.0
    for target, cost in by_target.items():
        if target != display_target:
            cost = _fx_convert(cost, target, display_target, fx_rates)
        total += cost
    return _fmt_cost_val(_symbol_for(display_target), total)


def fmt_last_cost(
    model_id: str,
    per_call_input: int,
    per_call_output: int,
    per_call_cache_read: int,
    per_call_cache_write: int = 0,
) -> str:
    """Cost of the most recent turn for a single model.

    The cost is computed in the model's resolved price currency, then
    FX-converted (if needed) to the model's *target display currency*:

      • If the model declares ``currency: X``, the LAST line shows in
        ``X`` — the user always sees the per-model native price.
      • If no ``currency`` field is set, the LAST line follows the
        user's ``display_currency`` (FX from base if they differ).
    """
    base, display, fx_rates = _currency_settings()
    price, price_currency, target = _resolve_price(model_id, display, base)
    cost_native = _model_cost_in_native(price, {
        "input": per_call_input,
        "output": per_call_output,
        "cache_read": per_call_cache_read,
        "cache_write": per_call_cache_write,
    })
    if price_currency != target:
        cost = _fx_convert(cost_native, price_currency, target, fx_rates)
    else:
        cost = cost_native
    if cost < 0.0001:
        return f"{_symbol_for(target)}0"
    return _fmt_cost_val(_symbol_for(target), cost)
