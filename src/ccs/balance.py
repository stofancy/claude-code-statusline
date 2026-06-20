"""Multi-provider balance/credits/quota query module.

Each provider implements its own query function. The module dispatches based
on the model ID to the appropriate provider. Results are cached to disk with
configurable TTL (default 5 min) to avoid rate limiting.

Supported providers:
  - DeepSeek: official ``GET /user/balance`` (API key via ``DEEPSEEK_API_KEY``)
  - OpenAI:   undocumented ``GET /dashboard/billing/credit_grants`` (browser session key via ``OPENAI_SESSION_KEY``)
  - MiniMax:  ``GET /v1/token_plan/remains`` — quota-based (auto-discovers token from mmx CLI state / env)
  - Zhipu:    ``GET /api/monitor/usage/quota/limit`` — quota-based (API key via ``ZHIPU_API_KEY`` or ``ANTHROPIC_AUTH_TOKEN``)
  - Anthropic: delegated to ``usage.py`` (not duplicated here)

Set ``CCS_BALANCE_API=0`` to disable all network requests.
Set ``CCS_BALANCE_TTL=<seconds>`` to adjust cache refresh interval (min 180).
"""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

_CACHE_DIR = Path.home() / ".claude" / "statusline"
_CACHE_PATH = _CACHE_DIR / "balance_cache.json"

_DEFAULT_TTL = 300
_MIN_TTL = 180
_RETRY_BACKOFF = 60
_HTTP_TIMEOUT = 4.0

_DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
_OPENAI_CREDIT_URL = "https://api.openai.com/dashboard/billing/credit_grants"
_ZHIPU_QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"

# 进程内内存缓存（TTL 内不读磁盘，不发起网络请求）
_mem_cache: dict | None = None
_mem_cache_provider: str | None = None
_mem_cache_fetched: int = 0


def _ttl() -> int:
    raw = os.getenv("CCS_BALANCE_TTL", "").strip()
    if raw:
        try:
            return max(_MIN_TTL, int(raw))
        except ValueError:
            pass
    return _DEFAULT_TTL


def _enabled() -> bool:
    return os.getenv("CCS_BALANCE_API", "1").strip().lower() not in (
        "0", "false", "no", "off"
    )


def _read_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except OSError:
        pass


def _detect_proxy_provider() -> str | None:
    """检测是否通过代理使用非 Anthropic 提供商。

    当 ``ANTHROPIC_BASE_URL`` 指向非官方地址时（如本地代理 127.0.0.1），
    通过 ``ANTHROPIC_DEFAULT_OPUS_MODEL_NAME`` 反推实际后端提供商。
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not base_url or "api.anthropic.com" in base_url:
        return None

    model_name = (os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL_NAME", "")
                  or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME", "")
                  or "").lower()
    if "deepseek" in model_name:
        return "deepseek"
    if "gpt" in model_name:
        return "openai"
    if "gemini" in model_name:
        return "google"
    if "mimo" in model_name:
        return "mimo"
    if "minimax" in model_name:
        return "minimax"
    if "zhipu" in model_name or "glm" in model_name:
        return "zhipu"
    return None


def _strip_suffix(model_id: str) -> str:
    """统一剥离模型 ID 中的 ``[1m]`` / ``[1M]`` 后缀。

    两种来源共享同一后缀形态：
    - 缓存变体标识（MiniMax/DeepSeek 的 1-hour cache hint）
    - 1M 上下文窗口标识（Anthropic Claude Code 的 1M context 变体）
    """
    if not model_id:
        return ""
    return model_id.replace("[1m]", "").replace("[1M]", "").strip()


def resolve_actual_model_id(mapped_model_id: str) -> str:
    """将代理映射后的模型 ID 解析为实际模型名。

    当通过代理使用非 Anthropic 模型时（如用 DeepSeek 替代 Claude），
    model_id 是 ``claude-sonnet-4-6[1M]``，但实际价格和显示都应对应
    ``deepseek-v4-flash``。此函数通过 ``ANTHROPIC_DEFAULT_*_MODEL_NAME``
    环境变量解析真实模型 ID。

    非代理模式：剥离后缀后原样返回 ``mapped_model_id``。
    """
    if not mapped_model_id:
        return ""
    if not _detect_proxy_provider():
        return _strip_suffix(mapped_model_id)

    # 如果 model_id 已经是非 Claude 模型（如 deepseek-v4-flash）、
    # 说明客户端已直接使用实际模型而非通过代理映射，无需再做 tier 解析。
    if "claude" not in mapped_model_id.lower():
        return _strip_suffix(mapped_model_id)

    mid = mapped_model_id.lower().replace("[1m]", "").replace("[1M]", "")

    # 按 tier 映射：claude-opus→OPUS, claude-sonnet→SONNET, claude-haiku→HAIKU
    _TIER_ENV_MAP = [
        ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"),
        ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME"),
        ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME"),
    ]
    for keyword, env_key in _TIER_ENV_MAP:
        if keyword in mid:
            actual = os.environ.get(env_key, "").strip()
            if actual:
                return _strip_suffix(actual)

    # 后备：用 OPUS 模型名
    return _strip_suffix(
        os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL_NAME", mapped_model_id)
    )


def provider_for(model_id: str) -> str | None:
    """公开函数：判断提供的 model_id 对应哪个提供商的余额/配额查询。

    当检测到代理模式时，即使 model_id 为 claude*，也返回实际后端提供商。
    Claude 官方模型（非代理）返回 ``"anthropic"``，无匹配返回 ``None``。
    """
    if not model_id:
        return None
    mid = model_id.lower()
    if "claude" in mid:
        return _detect_proxy_provider() or "anthropic"
    if "deepseek" in mid:
        return "deepseek"
    if "gpt-" in mid or mid.startswith("o"):
        return "openai"
    if "gemini" in mid:
        return "google"
    if "mimo" in mid:
        return "mimo"
    if "minimax" in mid:
        return "minimax"
    if "zhipu" in mid or "glm" in mid:
        return "zhipu"
    return None


# ── DeepSeek ──────────────────────────────────────────────────────────────

def _deepseek_balance() -> dict | None:
    """Query DeepSeek account balance.

    Endpoint: GET https://api.deepseek.com/user/balance
    Auth: ``Authorization: Bearer <DEEPSEEK_API_KEY>``
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None

    req = urllib.request.Request(
        _DEEPSEEK_BALANCE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data: dict = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError):
        return None

    if not data.get("is_available"):
        return None

    infos = data.get("balance_infos", [])
    if not infos:
        return None

    info = infos[0]
    return {
        "provider": "deepseek",
        "type": "monetary",
        "currency": info.get("currency", "CNY"),
        "balance": float(info.get("total_balance", 0)),
        "granted": float(info.get("granted_balance", 0)),
        "topped_up": float(info.get("topped_up_balance", 0)),
    }


# ── OpenAI ────────────────────────────────────────────────────────────────

def _openai_balance() -> dict | None:
    """Query OpenAI credit grants (remaining credits).

    Endpoint: GET https://api.openai.com/dashboard/billing/credit_grants
    Auth: ``Authorization: Bearer <OPENAI_SESSION_KEY>``
    Note: Requires a *browser session token* (``sess-...``), NOT an API key.
    Session tokens expire every ~2–14 days and must be re-extracted from the
    OpenAI billing dashboard in a web browser.
    """
    session_key = os.getenv("OPENAI_SESSION_KEY", "").strip()
    if not session_key:
        return None

    req = urllib.request.Request(
        _OPENAI_CREDIT_URL,
        headers={
            "Authorization": f"Bearer {session_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data: dict = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError):
        return None

    return {
        "provider": "openai",
        "type": "credits",
        "currency": "USD",
        "balance": float(data.get("total_available", 0)),
        "total_granted": float(data.get("total_granted", 0)),
        "total_used": float(data.get("total_used", 0)),
    }


# ── MiniMax ───────────────────────────────────────────────────────────────

def _minimax_token() -> str | None:
    """Discover MiniMax API token from env var or mmx CLI state files."""
    env_tok = os.getenv("MINIMAX_API_KEY", "").strip()
    if env_tok:
        return env_tok

    mmx_state = Path.home() / ".mmx" / "state.json"
    try:
        state = json.loads(mmx_state.read_text(encoding="utf-8"))
        tok = state.get("accessToken") or state.get("apiKey")
        if tok:
            return tok.strip()
    except (OSError, ValueError):
        pass

    creds_file = Path.home() / ".minimaxi" / "credentials.json"
    try:
        creds = json.loads(creds_file.read_text(encoding="utf-8"))
        tok = creds.get("api_key") or creds.get("token") or creds.get("accessToken")
        if tok:
            return tok.strip()
    except (OSError, ValueError):
        pass

    return None


def _minimax_balance() -> dict | None:
    """Query MiniMax per-model quota (not monetary balance).

    Endpoint: GET {base}/v1/token_plan/remains
    Returns per-model remaining percentage in the current interval.
    """
    token = _minimax_token()
    if not token:
        return None

    # Try China region first (most common for MiniMax users).
    # If it fails, the cache backoff handles retries.
    base = "https://api.minimaxi.com"
    url = f"{base}/v1/token_plan/remains"

    # Try both auth styles that mmx CLI supports
    data = None
    for header_key, header_val in [
        ("Authorization", f"Bearer {token}"),
        ("x-api-key", token),
    ]:
        req = urllib.request.Request(
            url,
            headers={header_key: header_val, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except (urllib.error.URLError, ValueError, OSError):
            continue

    if not data or not isinstance(data, dict):
        return None

    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code") != 0:
        return None

    models = data.get("model_remains", [])
    if not models:
        return None

    quotas = []
    for m in models:
        quotas.append({
            "name": m.get("model_name", "?"),
            "remaining_pct": m.get("current_interval_remaining_percent", 0),
            "status": m.get("current_interval_status", 0),
        })

    return {
        "provider": "minimax",
        "type": "quota",
        "quotas": quotas,
    }


# ── Zhipu (GLM) ─────────────────────────────────────────────────────────────

def _glm_api_key() -> str | None:
    """获取智谱 API Key。

    Claude Code 兼容环境下，统一认证变量是 ``ANTHROPIC_AUTH_TOKEN`` /
    ``ANTHROPIC_API_KEY``（而非 provider 专用的 ``ZHIPU_API_KEY``）——通过 cc-switch
    等代理或直连智谱时，真实 key 通常就在这两个变量里。查找优先级：
    ``ZHIPU_API_KEY`` → ``ANTHROPIC_AUTH_TOKEN`` → ``ANTHROPIC_API_KEY``。
    """
    for var in ("ZHIPU_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        val = os.getenv(var, "").strip()
        if val:
            return val
    return None


def _glm_window_quota(short: str, item: dict) -> dict:
    """百分比窗口型配额（5H / 7D）：``percentage``=已用%，``nextResetTime`` 毫秒→秒。"""
    nrt = item.get("nextResetTime") or 0
    return {
        "kind": "window",
        "short": short,
        "used_pct": float(item.get("percentage", 0) or 0),
        "resets_at": int(nrt // 1000) if nrt else None,
    }


def _glm_balance() -> dict | None:
    """查询智谱 GLM Coding Plan 配额用量。

    Endpoint: GET https://open.bigmodel.cn/api/monitor/usage/quota/limit
    Auth: 先试 ``Authorization: Bearer <key>``（社区高赞 cc-switch #880），无效再试裸
    ``<key>``（#1588）—— 参照 MiniMax 双 header 范式。

    真实返回结构（cc-switch issue #1588 验证）::

        {"code":200,"success":true,"data":{
          "level":"pro",
          "limits":[
            {"type":"TOKENS_LIMIT","percentage":<已用%>,"nextResetTime":<毫秒>},   # 1~2 条
            {"type":"TIME_LIMIT","usage":<总额>,"currentValue":<已用>,
             "remaining":<剩余>,"nextResetTime":<毫秒>}                          # MCP 每月
          ]}}

    归一化为 ``type="plan"``（与 MiniMax 的 ``"quota"`` 隔离）：仅取 TOKENS_LIMIT
    按 ``nextResetTime`` 升序 → 5H(最早重置) / 7D 百分比窗口；TIME_LIMIT 是 MCP
    工具余额，不显示。``percentage`` 已验证为「已用」百分比。
    """
    api_key = _glm_api_key()
    if not api_key:
        return None

    # 认证双试：智谱对无效 token 返回 HTTP 200 + body{code:401,success:false}，
    # 不能靠异常判断，需读 body 校验 success。
    data: dict | None = None
    for auth_val in (f"Bearer {api_key}", api_key):
        req = urllib.request.Request(
            _ZHIPU_QUOTA_URL,
            headers={"Authorization": auth_val, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                candidate = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, OSError):
            continue
        if isinstance(candidate, dict) and candidate.get("success") and candidate.get("code") == 200:
            data = candidate
            break

    if not isinstance(data, dict) or not data.get("success") or data.get("code") != 200:
        return None

    nested = data.get("data")
    if not isinstance(nested, dict):
        return None

    limits = nested.get("limits")
    if not isinstance(limits, list) or not limits:
        return None

    # 仅 token 配额窗口（5H/7D）；TIME_LIMIT 是 MCP 工具余额，不显示。
    token_limits = sorted(
        [l for l in limits if isinstance(l, dict) and l.get("type") == "TOKENS_LIMIT"],
        key=lambda l: l.get("nextResetTime") or 0,
    )

    quotas: list[dict] = []
    if len(token_limits) >= 1:
        quotas.append(_glm_window_quota("5H", token_limits[0]))
    if len(token_limits) >= 2:
        quotas.append(_glm_window_quota("7D", token_limits[1]))

    if not quotas:
        return None

    return {
        "provider": "zhipu",
        "type": "plan",
        "quotas": quotas,
    }


# ── Dispatch ──────────────────────────────────────────────────────────────

_QUERY_FUNCS: dict[str, callable] = {
    "deepseek": _deepseek_balance,
    "openai": _openai_balance,
    "minimax": _minimax_balance,
    "zhipu": _glm_balance,
}


def _fetch_balance(provider: str) -> dict | None:
    fn = _QUERY_FUNCS.get(provider)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


def get_balance(model_id: str) -> dict | None:
    """Return normalized balance data for the provider of ``model_id``.

    Returns the same normalized dict as the provider-specific function,
    or ``None`` when the provider has no balance API / credentials are
    missing / the request failed.

    Three-tier cache (fast→slow):
      1. **进程内内存缓存** — TTL 内直接返回，零 I/O。
      2. **磁盘缓存** — 无内存缓存或 provider 切换时读取。
      3. **网络请求** — 内存和磁盘均过期时发起一次同步 HTTP 请求。

    TTL 默认 300s（最低 180s），失败按 60s 退避。
    """
    if not _enabled():
        return None

    provider = provider_for(model_id)
    if provider is None or provider == "anthropic":
        return None

    now = int(time.time())
    ttl = _ttl()

    # Tier 1: 进程内内存缓存（最快，零 I/O）
    global _mem_cache, _mem_cache_provider, _mem_cache_fetched
    if (_mem_cache is not None
            and _mem_cache_provider == provider
            and (now - _mem_cache_fetched) < ttl):
        return _mem_cache

    # Tier 2: 磁盘缓存
    cache = _read_cache()
    cached_data = cache.get("data")
    cached_provider = cache.get("provider")
    fetched_at = cache.get("fetched_at", 0)
    last_attempt = cache.get("last_attempt", 0)

    fresh = (
        cached_data is not None
        and cached_provider == provider
        and (now - fetched_at) < ttl
    )
    backoff = cached_data is not None and (now - last_attempt) < _RETRY_BACKOFF

    if fresh or backoff:
        _mem_cache, _mem_cache_provider, _mem_cache_fetched = cached_data, provider, now
        return cached_data

    # Tier 3: 网络请求
    data = _fetch_balance(provider)
    if data is not None:
        _write_cache({
            "fetched_at": now,
            "last_attempt": now,
            "provider": provider,
            "data": data,
        })
        _mem_cache, _mem_cache_provider, _mem_cache_fetched = data, provider, now
        return data

    cache["last_attempt"] = now
    _write_cache(cache)
    return cached_data
