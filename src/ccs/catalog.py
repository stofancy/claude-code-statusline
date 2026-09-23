"""Remote pricing catalog: models.dev model prices + daily FX rates.

Two small on-disk caches under ``~/.claude/statusline/``:

  • ``models_dev.json`` — compacted from https://models.dev/api.json into a
    flat ``{model_id_casefold: price_block}`` map already in our
    ``*_per_1m`` schema (plus ``tiers`` for context-length pricing).
  • ``fx_rates.json``   — units of currency per 1 USD.

Refresh policy
--------------
The status line never waits on the network for these. On every tick
:func:`maybe_refresh` only stats the cache files; when one is older than
24h it spawns a detached ``python -m ccs.catalog`` worker and returns immediately. A lock file doubles
as a 1h backoff so a failing network does not spawn a worker per tick.
models.dev supports ETag, so an unchanged catalog costs a 304.

Only model-author catalogs (``_AUTHORED_PROVIDERS``) are used: models.dev
also lists every model once per reseller that serves it, at marked-up or
subscription ($0) prices.

Set ``CCS_PRICING_API=0`` to disable both fetching and use of these caches
(pricing then comes from the YAML tables only).
"""

import gzip
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

_CACHE_DIR = Path.home() / ".claude" / "statusline"
_MODELS_PATH = _CACHE_DIR / "models_dev.json"
_FX_PATH = _CACHE_DIR / "fx_rates.json"
_LOCK_PATH = _CACHE_DIR / ".catalog_refresh.lock"

_MODELS_URL = "https://models.dev/api.json"
_FX_URL = "https://open.er-api.com/v6/latest/USD"

_TTL = 86400
_RETRY_BACKOFF = 3600
_HTTP_TIMEOUT = 30.0
_USER_AGENT = f"claude-code-statusline/{__version__}"

# 模型原厂目录，按优先级排列（同名 id 先到先得）。zai 为智谱国际站，
# 排在 zhipuai 前；二者价格均为 USD。
_AUTHORED_PROVIDERS = (
    "anthropic", "openai", "google", "xai", "deepseek", "minimax",
    "zai", "zhipuai", "xiaomi", "moonshotai", "mistral", "alibaba", "cohere",
)
_mem: dict[Path, tuple[float, dict]] = {}


def _enabled() -> bool:
    return os.getenv("CCS_PRICING_API", "1").strip().lower() not in ("0", "false", "no", "off")


# ── Cache I/O ─────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict | None:
    """Read a cache file, memoized per process by mtime."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _mem.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    _mem[path] = (mtime, data)
    return data


def _write_json(path: Path, data: dict) -> None:
    """Atomic write so a concurrent reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _stale(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime >= _TTL
    except OSError:
        return True


# ── Public read API ───────────────────────────────────────────────────────

def lookup(model_id: str) -> dict | None:
    """Return the models.dev price block for ``model_id`` (case-insensitive)."""
    if not model_id or not _enabled():
        return None
    data = _load_json(_MODELS_PATH)
    models = data.get("models") if data else None
    if not isinstance(models, dict):
        return None
    block = models.get(model_id.casefold())
    return block if isinstance(block, dict) else None


def fx_rates() -> dict | None:
    """Return the fetched ``{currency: units_per_usd}`` map, or ``None``."""
    if not _enabled():
        return None
    data = _load_json(_FX_PATH)
    rates = data.get("rates") if data else None
    return rates if isinstance(rates, dict) and rates else None


# ── models.dev compaction ─────────────────────────────────────────────────

def _rates(src: dict) -> dict | None:
    if not isinstance(src.get("input"), (int, float)) or not isinstance(src.get("output"), (int, float)):
        return None
    block = {"input_per_1m": float(src["input"]), "output_per_1m": float(src["output"])}
    if isinstance(src.get("cache_read"), (int, float)):
        block["cache_read_per_1m"] = float(src["cache_read"])
    if isinstance(src.get("cache_write"), (int, float)):
        block["cache_write_per_1m"] = float(src["cache_write"])
    return block


def _convert_cost(cost) -> dict | None:
    """Translate a models.dev ``cost`` object into our price-block schema.

    Context-length tiers become ``tiers: [{above: N, ...rates}]`` — the tier
    applies when the call's prompt exceeds ``N`` tokens. ``tiers`` is
    preferred over the legacy ``context_over_200k`` key, which models.dev
    also fills for thresholds that are not 200k (e.g. GPT-5.4 at 272k).
    """
    if not isinstance(cost, dict):
        return None
    block = _rates(cost)
    if block is None:
        return None
    tiers = []
    for t in cost.get("tiers") or []:
        if not isinstance(t, dict):
            continue
        spec = t.get("tier") or {}
        if spec.get("type") != "context" or not isinstance(spec.get("size"), (int, float)):
            continue
        tb = _rates(t)
        if tb is not None:
            tb["above"] = int(spec["size"])
            tiers.append(tb)
    if not tiers and isinstance(cost.get("context_over_200k"), dict):
        tb = _rates(cost["context_over_200k"])
        if tb is not None:
            tb["above"] = 200_000
            tiers.append(tb)
    if tiers:
        block["tiers"] = sorted(tiers, key=lambda t: t["above"])
    return block


def compact(raw: dict) -> dict[str, dict]:
    """Flatten models.dev ``api.json`` into ``{model_id_casefold: block}``."""
    out: dict[str, dict] = {}
    for pid in _AUTHORED_PROVIDERS:
        provider = raw.get(pid)
        models = provider.get("models") if isinstance(provider, dict) else None
        for mid, m in (models or {}).items():
            block = _convert_cost(m.get("cost") if isinstance(m, dict) else None)
            if block is not None:
                out.setdefault(mid.casefold(), block)
    return out


# ── Network refresh ───────────────────────────────────────────────────────

def _http_get(url: str, headers: dict | None = None):
    """Return ``(status, body, headers)``; a 304 is returned, not raised."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept-Encoding": "gzip",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
            return resp.status, body, resp.headers
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", e.headers
        raise


def refresh_models(force: bool = False) -> bool:
    cached = _load_json(_MODELS_PATH)
    etag = cached.get("etag") if cached and cached.get("models") else None
    headers = {"If-None-Match": etag} if etag and not force else None
    status, body, resp_headers = _http_get(_MODELS_URL, headers)
    now = int(time.time())
    if status == 304 and cached:
        # 未变化：只刷新时间戳（重写文件以更新 mtime，staleness 以 mtime 为准）
        _write_json(_MODELS_PATH, {**cached, "fetched_at": now})
        return True
    models = compact(json.loads(body))
    if not models:
        return False
    _write_json(_MODELS_PATH, {
        "version": 1,
        "source": _MODELS_URL,
        "fetched_at": now,
        "etag": resp_headers.get("ETag"),
        "models": models,
    })
    return True


def refresh_fx() -> bool:
    _, body, _ = _http_get(_FX_URL)
    rates = json.loads(body).get("rates")
    if not isinstance(rates, dict):
        return False
    clean = {k.upper(): float(v) for k, v in rates.items() if isinstance(v, (int, float)) and v > 0}
    _write_json(_FX_PATH, {"fetched_at": int(time.time()), "source": _FX_URL, "rates": clean})
    return True


def refresh(force: bool = False) -> dict[str, bool | None]:
    """Refresh stale caches (all with ``force``). ``None`` = skipped (fresh)."""
    results: dict[str, bool | None] = {}
    jobs = (("models", _MODELS_PATH, lambda: refresh_models(force)),
            ("fx", _FX_PATH, refresh_fx))
    for name, path, fn in jobs:
        if not force and not _stale(path):
            results[name] = None
            continue
        try:
            results[name] = fn()
        except Exception:
            results[name] = False
    return results


def _spawn_refresh() -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "ccs.catalog"], **kwargs)


def maybe_refresh() -> bool:
    """Spawn a background refresh when a cache is stale. Never blocks.

    Returns ``True`` when a worker was spawned.
    """
    if not _enabled():
        return False
    if not (_stale(_MODELS_PATH) or _stale(_FX_PATH)):
        return False
    try:
        if time.time() - _LOCK_PATH.stat().st_mtime < _RETRY_BACKOFF:
            return False
    except OSError:
        pass
    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LOCK_PATH.touch()
        _spawn_refresh()
    except Exception:
        return False
    return True


def main() -> None:
    force = "--force" in sys.argv[1:]
    results = refresh(force=force)
    for name, ok in results.items():
        state = "fresh" if ok is None else ("updated" if ok else "failed")
        print(f"{name}: {state}")
    data = _load_json(_MODELS_PATH)
    if data and isinstance(data.get("models"), dict):
        print(f"models.dev entries: {len(data['models'])}")
    rates = fx_rates()
    if rates:
        print(f"USD→CNY: {rates.get('CNY')}")


if __name__ == "__main__":
    main()
