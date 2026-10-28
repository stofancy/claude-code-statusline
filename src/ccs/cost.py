"""Multi-provider cost calculation with configurable pricing table.

All cumulative cost goes through :func:`fmt_cost_multi`, which uses actual
per-model token breakdowns from ``model_usage`` (not ratio-based estimation).

Currency model
--------------
**一次会话只显示一种货币，全程不换算。** 人民币定价的模型同时带
``prices.CNY`` 与 ``prices.USD``（各厂商两套官方/目录价并行存在）；只要会话里
有一个模型没有人民币价，整行按美元显示，其它模型取各自的美元块。所以既不会
出现 ``¥1.00 + $3.00`` 这样的混排，也不会把美元价贴上 ``¥``。

A model block publishes prices as a multi-currency ``prices`` map::

    minimax:
      MiniMax-M3:
        prices:
          CNY: { input_per_1m: 2.10, output_per_1m: 8.40 }
          USD: { input_per_1m: 0.30, output_per_1m: 1.20 }

A legacy flat dict has no ``prices`` map and is USD.

Resolution order for the *active* price block:

  1. the session currency, when the block carries it (``CNY`` or ``USD``);
  2. otherwise ``prices["CNY"]`` — 人民币块；
  3. otherwise ``prices["USD"]``；
  4. otherwise the first available key；
  5. flat dict → USD.
"""

import os
import re
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

import yaml
from rapidfuzz import fuzz, process

from . import catalog

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

# 把版本号中的点号改写为短横线：``claude-opus-4.8`` → ``claude-opus-4-8``。
# 仅匹配「数字.数字」边界，确保只动版本分隔符，不影响 id 其它位置可能
# 合法出现的点号。背景：Claude Code 写入 JSONL transcript 的
# ``message.model`` 用点号形式（``claude-opus-4.8`` / ``claude-haiku-4.5``），
# 而定价表键名用短横线形式（``claude-opus-4-8``）。不归一化则点号形式
# 永远命中不到价表、落入默认定价（约 10 倍少算）。
_DOT_VERSION_RE = re.compile(r"(?<=\d)\.(?=\d)")

_BUILTIN_PRICING = Path(__file__).parent / "pricing.yaml"
_USER_PRICING = Path.home() / ".claude" / "statusline" / "pricing.yaml"
# 默认定价（未知模型的后备）。注意：不包含 cache_write_per_1m，
# 因为该字段是 Anthropic 专属的（1.25× input），对未知提供商不应假定。
_DEFAULT_PRICING = {"input_per_1m": 1.0, "output_per_1m": 4.0, "cache_read_per_1m": 0.1}

_CURRENCY_SYMBOLS = {
    "USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£", "JPY": "¥",
    "AUD": "A$", "INR": "₹", "HKD": "HK$", "SGD": "S$", "KRW": "₩",
    "CAD": "C$", "CHF": "CHF", "TWD": "NT$",
}

_DEBUG_LOG = Path.home() / ".claude" / "statusline" / "debug.log"

# 模型 id 的模糊匹配把它切成 token 再比较：``newapi/deepseek-v4-flash-v2``
# → (newapi, deepseek, v4, flash, v2)。分隔符里包含 ``.`` 与 ``_``，是因为
# 同一版本号在不同来源写成 ``4.8`` / ``4-8`` / ``4_8`` 都有实测例子。
_TOKEN_SPLIT_RE = re.compile(r"[/\-_.\s]+")

# 「版本 token」= 纯数字及其带 v 前缀/点号的写法（``4``、``v4``、``3.2``、
# ``20250714``）。版本 token 在模糊匹配里是关键 token：两侧都有且不相交时
# 视为不同代模型，拒绝匹配（否则 ``deepseek-v4-flash`` 会吃到 v3 的价）。
_VERSION_TOKEN_RE = re.compile(r"v?\d+(?:[._]\d+)*", re.IGNORECASE)

# pricing.yaml 的顶层配置键，不是模型条目，模糊匹配枚举时要跳过。
# 币种设置键（fx_rates / base_currency / display_currency / default_currency）
# 已经退役，仍列在这里只是为了不把老用户文件里的残留键当成模型名。
_SETTING_KEYS = frozenset({
    "providers", "patterns", "fx_rates",
    "base_currency", "display_currency", "default_currency",
})

# rapidfuzz token_set_ratio 下限。实测：token 子集过滤后，正确匹配最低 50
# （`relay-MiniMax-M3-20250101` → MiniMax-M3），而无关 id 在过滤阶段已全空，
# 因此 50 只拦「部分 token 撞上但明显不是同一模型」的情况。
_FUZZY_CUTOFF = 50.0

# 带这些键的 YAML 条目表达了 models.dev 无法表达的信息（原生币种、峰谷、
# 自定义分档），同一候选 id 下优先于 models.dev。
_PINNED_KEYS = ("prices", "tide", "tiers")

# 分档合并时按「同义键组」整组替换，避免 tier 的 cache_read_per_1m 被基础块
# 残留的 input_cache_hit_per_1m（优先级更高的别名）遮蔽。
_PRICE_KEY_GROUPS = (
    ("input_per_1m", "input"),
    ("output_per_1m", "output"),
    ("input_cache_hit_per_1m", "cache_read_per_1m"),
    ("cache_write_per_1m",),
)

_yaml_cache: dict[Path, tuple[float, dict]] = {}
_pricing_cache: dict | None = None
_pricing_key: tuple | None = None
_user_entry_ids: set[int] = set()
_unresolved_logged: set[str] = set()
_matched_logged: set[str] = set()


def _read_yaml(path: Path) -> tuple[float, dict]:
    """Return ``(mtime, data)``; re-reads on mtime change. Missing → ``(0, {})``."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0, {}
    hit = _yaml_cache.get(path)
    if hit is not None and hit[0] == mtime:
        return hit
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _yaml_cache[path] = (mtime, data)
    return mtime, data


def _iter_model_entries(cfg: dict):
    for k, v in cfg.items():
        if k == "models" and isinstance(v, dict):
            yield from (e for e in v.values() if isinstance(e, dict))
        elif isinstance(v, dict):
            yield v


def _load_pricing() -> dict:
    """Builtin pricing table overlaid by the user's ``pricing.yaml``.

    The user file no longer has to be a full copy: top-level settings
    override the builtin ones, user provider namespaces are searched first,
    and a same-named namespace is merged model by model. Entries that come
    from the user file are recorded in ``_user_entry_ids`` so they always win
    over models.dev.
    """
    global _pricing_cache, _pricing_key, _user_entry_ids

    user_mtime, user = _read_yaml(_USER_PRICING)
    builtin_mtime, builtin = _read_yaml(_BUILTIN_PRICING)
    key = (user_mtime, builtin_mtime)
    if _pricing_cache is not None and _pricing_key == key:
        return _pricing_cache

    merged = {k: v for k, v in builtin.items() if k != "providers"}
    merged.update({k: v for k, v in user.items() if k != "providers"})

    user_providers = user.get("providers") or {}
    providers: dict = {}
    for name, cfg in user_providers.items():
        providers[name] = cfg
    for name, cfg in (builtin.get("providers") or {}).items():
        if name not in providers:
            providers[name] = cfg
        elif isinstance(cfg, dict) and isinstance(providers[name], dict):
            providers[name] = {**cfg, **providers[name]}
    merged["providers"] = providers

    user_ids = {id(e) for cfg in user_providers.values() if isinstance(cfg, dict)
                for e in _iter_model_entries(cfg)}
    user_ids |= {id(v) for k, v in user.items()
                 if k not in _SETTING_KEYS and isinstance(v, dict)}

    _user_entry_ids = user_ids
    _pricing_cache = merged
    _pricing_key = key
    return _pricing_cache


def _is_pinned(entry: dict) -> bool:
    return id(entry) in _user_entry_ids or any(k in entry for k in _PINNED_KEYS)


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


def _debug_match(model_id: str, matched: str, how: str) -> None:
    """Log a non-exact resolution once per process when CCS_DEBUG=1."""
    if os.getenv("CCS_DEBUG") != "1":
        return
    tag = f"{how}:{model_id}"
    if tag in _matched_logged:
        return
    _matched_logged.add(tag)
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[cost] {how} match: {model_id} -> {matched}\n")
    except OSError:
        pass


def _tokens(model_id: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN_SPLIT_RE.split(model_id.casefold()) if t)


def _version_tokens(tokens: tuple[str, ...]) -> frozenset[str]:
    return frozenset(t for t in tokens if _VERSION_TOKEN_RE.fullmatch(t))


def _named_entries(pricing: dict):
    """Yield ``(key, entry, tier)`` for every priceable id, tier 0 = pinned.

    Tier ranks a match inside one fuzzy score band: pinned YAML (0) beats the
    models.dev catalog (1), which beats a plain offline YAML entry (2) — the
    same precedence the exact chain uses per candidate id.
    """
    providers = pricing.get("providers") or {}
    for cfg in providers.values():
        if not isinstance(cfg, dict):
            continue
        for k, v in cfg.items():
            if k != "models" and isinstance(v, dict):
                yield k, v, 0 if _is_pinned(v) else 2
        models = cfg.get("models")
        if isinstance(models, dict):
            for k, v in models.items():
                if isinstance(v, dict):
                    yield k, v, 0 if _is_pinned(v) else 2
    for k, v in pricing.items():
        if k not in _SETTING_KEYS and isinstance(v, dict):
            yield k, v, 0 if _is_pinned(v) else 2
    for k, v in catalog.models().items():
        yield k, v, 1


def _exact_chain(model_id: str, pricing: dict) -> tuple[dict, bool] | None:
    """Resolve via generated candidate ids (verbatim, prefix/suffix-stripped)."""
    for cand in _candidate_ids(model_id):
        local = _find_model_entry(cand, pricing)
        if local is not None and _is_pinned(local):
            return local, False
        found = catalog.lookup(cand)
        if found is not None:
            return found, True
        if local is not None:
            return local, False
    return None


def _pattern_match(model_id: str, pricing: dict) -> tuple[dict, bool] | None:
    """User-declared ``patterns:`` rules — first glob that matches wins.

    Rule shape (pricing.yaml): ``- match: "*/deepseek-flash*"`` /
    ``use: deepseek-v4-flash``. ``*`` also crosses ``/`` because ids on
    gateway proxies embed the vendor path (``newapi/deepseek-flash``). The
    ``use`` target goes through the normal exact chain, so it may name any
    key already in the table.
    """
    rules = pricing.get("patterns")
    if not isinstance(rules, list):
        return None
    target = model_id.casefold()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        pattern, use = rule.get("match"), rule.get("use")
        if not isinstance(pattern, str) or not isinstance(use, str):
            continue
        if not fnmatchcase(target, pattern.casefold()):
            continue
        hit = _exact_chain(use, pricing)
        if hit is not None:
            _debug_match(model_id, use, "pattern")
            return hit
    return None


def _fuzzy_match(model_id: str, pricing: dict) -> tuple[dict, bool] | None:
    """Last-resort match scored by rapidfuzz over token-compatible table keys.

    Two filters, then a score:

    1. **Token subset** — every token of the table key must appear in the id
       (order-free, so ``deepseek_v4_flash`` and ``deepseek.v4.flash`` work).
       This is what keeps extensions out: a ``claude-opus-4-1`` key cannot
       absorb a ``claude-opus-4`` request.
    2. **Version veto** — a key carrying a version token the id lacks is
       rejected, and so is a different generation (``v3`` vs ``v4``): those
       sit at different prices.
    3. ``fuzz.token_set_ratio`` (rapidfuzz) ranks the survivors, so gateway
       prefixes/suffixes and separator spelling only move the score, not the
       filter outcome. Below ``_FUZZY_CUTOFF`` nothing is returned.
    """
    want = _tokens(model_id)
    if len(want) < 2:
        return None
    want_set, want_versions = set(want), _version_tokens(want)
    pool: dict[str, tuple[dict, int]] = {}
    for key, entry, tier in _named_entries(pricing):
        have = _tokens(key)
        if len(have) < 2 or not set(have) <= want_set:
            continue
        have_versions = _version_tokens(have)
        if have_versions and not have_versions <= want_versions:
            continue
        pool.setdefault(key, (entry, tier))
    if not pool:
        return None
    best = process.extractOne(model_id, pool.keys(), scorer=fuzz.token_set_ratio,
                              score_cutoff=_FUZZY_CUTOFF)
    if best is None:
        return None
    _debug_match(model_id, best[0], "fuzzy")
    entry, tier = pool[best[0]]
    return entry, tier == 1


def _select_entry(model_id: str, pricing: dict) -> tuple[dict, bool] | None:
    """Resolve ``model_id`` to ``(entry, came_from_catalog)``.

    Precedence: explicit user ``patterns`` → exact candidate chain → token
    fuzzy match. ``None`` means no rule applies and default pricing is used.
    """
    for resolve in (_pattern_match, _exact_chain, _fuzzy_match):
        hit = resolve(model_id, pricing)
        if hit is not None:
            return hit
    return None


def _utc_moment(at_ts: str | None) -> datetime:
    """Parse a transcript ISO timestamp into an aware UTC datetime (or now).

    Transcript timestamps look like ``2026-08-15T17:48:42.980Z``. Unknown
    formats fall back to the current UTC time so tide pricing never throws.
    """
    if not at_ts:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(at_ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_hour(at_ts: str | None) -> int:
    """Extract the UTC hour from a transcript ISO timestamp (or now)."""
    return _utc_moment(at_ts).hour


def _select_tide_block(price: dict, tide: dict, at_ts: str | None) -> dict:
    """Pick the ``peak`` / ``off_peak`` sub-block for a tide container.

    ``tide.peak_hours_utc`` is a list of half-open UTC hour intervals
    ``[start, end)`` (DeepSeek: ``[[1,4],[6,10]]``). Peak pricing applies on
    weekdays only: the vendor's rule is Monday–Friday Beijing time, and every
    peak window (01:00–04:00, 06:00–10:00 UTC) falls in the same Beijing
    calendar day, so the UTC weekday is the Beijing weekday throughout.

    Chinese public holidays are also off-peak on the vendor's side; they are
    not modeled here (no holiday calendar), so holiday calls inside a peak
    window are billed at the peak rate. A flat price block (no
    ``peak``/``off_peak`` keys) passes through unchanged.
    """
    if not isinstance(price, dict):
        return price
    peak = price.get("peak")
    off_peak = price.get("off_peak")
    if not (isinstance(peak, dict) and isinstance(off_peak, dict)):
        return price
    moment = _utc_moment(at_ts)
    if moment.weekday() >= 5:  # Saturday / Sunday
        return off_peak
    hour = moment.hour
    for start, end in (tide.get("peak_hours_utc") or []):
        if start <= hour < end:
            return peak
    return off_peak


def _select_tier_block(price: dict, prompt_tokens: int | None) -> dict:
    """Apply context-length tier pricing for one call.

    ``price.tiers`` is a list of ``{above: N, <rates>}``; the tier with the
    largest ``N`` below the call's prompt size (input + cache_read +
    cache_write) overrides the base rates. Without a per-call prompt size
    (aggregate pricing) the base tier is used.
    """
    tiers = price.get("tiers") if isinstance(price, dict) else None
    if not tiers or not prompt_tokens:
        return price
    chosen = None
    for t in tiers:
        if not isinstance(t, dict) or not isinstance(t.get("above"), (int, float)):
            continue
        if prompt_tokens > t["above"] and (chosen is None or t["above"] > chosen["above"]):
            chosen = t
    if chosen is None:
        return price
    merged = {k: v for k, v in price.items() if k != "tiers"}
    for group in _PRICE_KEY_GROUPS:
        if any(k in chosen for k in group):
            for k in group:
                merged.pop(k, None)
    merged.update({k: v for k, v in chosen.items() if k != "above"})
    return merged


def _candidate_ids(model_id: str) -> list[str]:
    """Lookup keys for ``model_id``, most specific first.

    Three spelling axes are normalised, then crossed so combined forms resolve:

    * ``[1m]`` / ``[1M]`` (even doubled) cache-variant suffixes are stripped.
    * Dot version separators become dashes (``claude-opus-4.8`` →
      ``claude-opus-4-8``); the dot form is kept as its own candidate because
      some table keys are dot-form (``grok-4.5``).
    * Proxy prefixes are stripped from the left (``newapi/x``,
      ``openrouter/vendor/x``) **and** variant/version suffixes are stripped
      from the right (``x-newapi``, ``x-v2``, ``x-20250714``).

    The two strip directions are applied independently per form, so
    ``newapi/deepseek-v4-flash-v2`` reaches ``deepseek-v4-flash`` — the old
    single-pass chain reached neither base and fell through to default
    pricing.
    """
    stripped = _CACHE_SUFFIX_RE.sub("", model_id)
    forms = [stripped]
    normalized = _DOT_VERSION_RE.sub("-", stripped)
    if normalized != stripped:
        forms.append(normalized)

    out: list[str] = []
    seen: set[str] = set()

    def add(cand: str) -> None:
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)

    for form in forms:
        segments = form.split("/")
        variants = [form] + ["/".join(segments[i:]) for i in range(1, len(segments))]
        for variant in variants:
            add(variant)
            parts = variant.split("-")
            while len(parts) > 1:
                parts.pop()
                add("-".join(parts))
    return out


def _resolve_price(
    model_id: str,
    at_ts: str | None = None,
    prompt_tokens: int | None = None,
    currency: str | None = None,
) -> tuple[dict, str]:
    """Return ``(price_dict, currency)`` — no currency conversion anywhere.

    ``currency`` is the currency to price in. ``None`` means the model's
    own preference (CNY block if present, USD otherwise). A session that
    contains any USD-only model passes ``"USD"`` so every model is priced
    from its dollar block — that keeps the whole line in one currency
    without inventing an exchange rate. When the requested currency is not
    in the entry, models.dev (USD) is consulted, then the default block.

    Source precedence per candidate id (most specific first, so a new
    ``claude-opus-5-5`` found on models.dev is not shadowed by the older
    ``claude-opus-5`` YAML entry that prefix stripping reaches):

      1. pinned YAML entry — from the user file, or carrying
         ``prices`` / ``tide`` / ``tiers``
      2. models.dev catalog (USD, refreshed daily)
      3. plain builtin YAML entry (offline snapshot)

    Matching layers, tried in order (:func:`_select_entry`):

      1. user ``patterns:`` globs — explicit override, first match wins
      2. exact candidate chain — ``[1m]`` / dot-version / prefix / suffix
         stripping (:func:`_candidate_ids`)
      3. rapidfuzz token match — keys whose tokens all appear in the id,
         same-version required (:func:`_fuzzy_match`)

    ``prompt_tokens`` (this call's input + cache_read + cache_write)
    selects the context-length tier, if the model has ``tiers``.
    """
    pricing = _load_pricing()
    selected = _select_entry(model_id, pricing)
    if selected is None:
        _debug_unresolved(model_id)
        return _DEFAULT_PRICING, "USD"
    entry, remote = selected

    if remote:
        # models.dev 一律美元标价
        return _select_tier_block(entry, prompt_tokens), "USD"

    price, price_currency = _select_price_block(entry, currency)
    if currency and price_currency != currency and currency == "USD":
        # 条目没有美元块：退到 models.dev 的美元价，避免同一行出现两种货币
        remote_price = catalog.lookup(model_id)
        if isinstance(remote_price, dict):
            return _select_tier_block(remote_price, prompt_tokens), "USD"
        return _DEFAULT_PRICING, "USD"
    tide = entry.get("tide")
    if isinstance(tide, dict):
        price = _select_tide_block(price, tide, at_ts)
    return _select_tier_block(price, prompt_tokens), price_currency


def _select_price_block(entry: dict, currency: str | None = None) -> tuple[dict, str]:
    """Pick the price block and its currency.

    Resolution order:

      1. ``prices[currency]`` — 会话币种，存在即用。
      2. ``prices["CNY"]`` — 人民币块。
      3. ``prices["USD"]`` — 美元块。
      4. First available key — last-resort fallback.

    Flat dict (no ``prices`` key) is a legacy entry priced in USD.

    A currency block may be a *tide container* — ``{peak: {...},
    off_peak: {...}}`` — for peak/off-peak pricing (DeepSeek 峰谷).
    The container is returned as-is; :func:`_select_tide_block` picks
    the sub-block by timestamp at resolve time.
    """
    prices_map = entry.get("prices")
    if isinstance(prices_map, dict) and prices_map:
        for cur in ((currency,) if currency else ()) + ("CNY", "USD"):
            block = prices_map.get(cur)
            if isinstance(block, dict):
                return block, cur
        for cur, block in prices_map.items():
            if isinstance(block, dict):
                return block, cur

    return entry, "USD"


def _symbol_for(currency: str) -> str:
    return _CURRENCY_SYMBOLS.get(currency, currency + " ")


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
        # usage["cache_write_1h"] 是 cache_write 中写入 1 小时缓存的部分；
        # Anthropic 的 1 小时缓存写入价为 2× 基础输入价（5 分钟为 1.25×）。
        cw_1h = min(usage.get("cache_write_1h", 0) or 0, cache_write)
        cw_1h_price = price.get("cache_write_1h_per_1m", input_price * 2)
        cost += (_per_1m(price["cache_write_per_1m"], cache_write - cw_1h)
                 + _per_1m(cw_1h_price, cw_1h))

    return cost


def _prompt_tokens(usage: dict) -> int:
    """Prompt size of one call — what context-length tiers are keyed on."""
    return (usage.get("input", 0) or 0) + (usage.get("cache_read", 0) or 0) + (usage.get("cache_write", 0) or 0)


def session_currency(
    model_breakdown: dict | None = None,
    model_calls: dict | None = None,
) -> str:
    """Currency for the whole session: ``CNY`` only if every model has one.

    A model that has no renminbi price (Claude, GPT, Gemini, Grok, Kimi …)
    forces the session to USD — otherwise the line would have to mix
    currencies or invent an exchange rate.
    """
    ids = list(model_calls or {}) or list(model_breakdown or {})
    for model_id in ids:
        _, cur = _resolve_price(model_id)
        if cur != "CNY":
            return "USD"
    return "CNY"


def fmt_cost_multi(
    model_breakdown: dict,
    primary_model_id: str | None = None,
    model_calls: dict | None = None,
    currency: str | None = None,
) -> str:
    """Render the cumulative cost string for a per-model usage breakdown.

    The whole session is priced in a single currency (:func:`session_currency`
    unless ``currency`` is given), so the result is always one number with one
    symbol — never ``¥1.00 + $3.00``, never an exchange rate.

    ``primary_model_id`` is kept for call-site compatibility; it no longer
    affects the currency.

    **Per-call precision (tide models):** pass ``model_calls`` as
    ``{model_id: [(iso_ts, usage), ...]}`` to price each API call with
    its own timestamp. For peak/off-peak (潮汐) models this is the only
    accurate path — an aggregate breakdown has no time information, so
    a mixed peak/off-peak session would otherwise be priced entirely at
    the current hour. When ``model_calls`` is given it takes precedence
    over ``model_breakdown`` for the models it covers. The same per-call
    path is what makes context-length tiers (``tiers``) exact: each call is
    priced at the tier its own prompt size falls into.
    """
    if not model_breakdown and not model_calls:
        return "-"

    cur = currency or session_currency(model_breakdown, model_calls)
    total = 0.0

    def _acc_cost(model_id: str, usage: dict, at_ts: str | None, prompt: int | None) -> None:
        nonlocal total
        price, _ = _resolve_price(model_id, at_ts=at_ts, prompt_tokens=prompt, currency=cur)
        total += _model_cost_in_native(price, usage)

    if model_calls:
        for model_id, calls in model_calls.items():
            for ts, usage in calls:
                _acc_cost(model_id, usage, ts, _prompt_tokens(usage))
    else:
        for model_id, usage in model_breakdown.items():
            _acc_cost(model_id, usage, None, None)

    return _fmt_cost_val(_symbol_for(cur), total)


def fmt_usd(amount: float) -> str:
    """Render a USD amount (e.g. Claude Code's own cost total)."""
    return _fmt_cost_val("$", amount)


def fmt_last_cost(
    model_id: str,
    per_call_input: int,
    per_call_output: int,
    per_call_cache_read: int,
    per_call_cache_write: int = 0,
    per_call_cache_write_1h: int = 0,
    currency: str | None = None,
) -> str:
    """Cost of the most recent turn for a single model.

    ``currency`` follows the session's currency so ``LAST`` and ``TOTAL``
    always show the same symbol.
    """
    usage = {
        "input": per_call_input,
        "output": per_call_output,
        "cache_read": per_call_cache_read,
        "cache_write": per_call_cache_write,
        "cache_write_1h": per_call_cache_write_1h,
    }
    price, cur = _resolve_price(model_id, prompt_tokens=_prompt_tokens(usage), currency=currency)
    cost = _model_cost_in_native(price, usage)
    if cost < 0.0001:
        return f"{_symbol_for(cur)}0"
    return _fmt_cost_val(_symbol_for(cur), cost)
