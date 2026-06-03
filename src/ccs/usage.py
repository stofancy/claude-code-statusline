"""Claude Code 官方订阅用量客户端。

读取驱动 `/usage` 命令的 OAuth 受保护端点 `GET /api/oauth/usage`，
暴露订阅的滚动限额窗口与月度 extra-usage 预算。该端点对调用方限流极其
激进（缺少 `claude-code` User-Agent 会落入严格的 429 桶，即便携带也建议
≥180s 轮询一次），因此结果按 TTL 缓存到磁盘，状态行每次 tick 复用缓存，
只在过期时才发起一次网络请求。

归一化两种截然不同的订阅形态：

- **额度窗口型**（team / pro / max）：`five_hour` / `seven_day` 等滚动窗口
  返回 0–100 的利用率百分比；`extra_usage` 以 credits 计量月度超额。
- **美元预算型**（enterprise，seat-based）：滚动窗口为 ``null``，仅
  `extra_usage` 以 USD 计量月度预算（`monthly_limit` / `used_credits`，
  单位为**美分**，例 ``25000`` == $250）；美分→美元换算在 renderer 层完成。

端点响应示例（enterprise 账户，滚动窗口全为 null）::

    {
      "five_hour": null,
      "seven_day": null,
      "seven_day_opus": null,
      "extra_usage": {
        "is_enabled": true,
        "monthly_limit": 25000,
        "used_credits": 8176.0,
        "utilization": 32.704,
        "currency": "USD",
        "disabled_reason": null
      }
    }

team / pro / max 账户的 `five_hour` / `seven_day` 形如
``{"utilization": 23.5, "resets_at": "2026-01-01T00:00:00Z"}``，
`extra_usage.currency` 通常不是 USD（按 credits 计）。
"""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA = "oauth-2025-04-20"
_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
_CACHE_PATH = Path.home() / ".claude" / "statusline" / "usage_cache.json"

# 端点限流：缺 UA 落入严格桶，携带 UA ≥180s 安全。默认 5 分钟刷新一次。
_DEFAULT_TTL = 300
_MIN_TTL = 180
# 失败后的退避：避免每次 tick 都重试一个正在 429/超时的端点。
_RETRY_BACKOFF = 60
_HTTP_TIMEOUT = 4.0
# 端点期望的 User-Agent；缺失时的兜底版本号。
_FALLBACK_VERSION = "2.1.161"


def _ttl() -> int:
    raw = os.getenv("CCS_USAGE_TTL", "").strip()
    if raw:
        try:
            return max(_MIN_TTL, int(raw))
        except ValueError:
            pass
    return _DEFAULT_TTL


def _enabled() -> bool:
    """官方用量查询默认开启，置 CCS_USAGE_API=0 关闭网络请求。"""
    return os.getenv("CCS_USAGE_API", "1").strip().lower() not in ("0", "false", "no", "off")


def _read_token() -> tuple[str | None, str | None]:
    """返回 (access_token, subscription_type)。

    解析顺序：环境变量 → ~/.claude/.credentials.json → macOS 钥匙串。
    任一来源失败都安静跳过。
    """
    env_tok = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok, None

    oauth = _read_credentials_blob()
    if oauth:
        tok = oauth.get("accessToken")
        if tok:
            return tok, oauth.get("subscriptionType")

    return None, None


def _read_credentials_blob() -> dict | None:
    """读取 claudeAiOauth 凭证块（Linux/Windows 文件 + macOS 钥匙串）。"""
    try:
        if _CREDENTIALS_PATH.exists():
            data = json.loads(_CREDENTIALS_PATH.read_text(encoding="utf-8"))
            blob = data.get("claudeAiOauth")
            if isinstance(blob, dict):
                return blob
    except (OSError, ValueError):
        pass

    # macOS：凭证存于钥匙串而非文件。
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout.strip())
            blob = data.get("claudeAiOauth")
            if isinstance(blob, dict):
                return blob
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    return None


def _iso_to_epoch(value) -> int | None:
    """把 ISO 8601（或已是 epoch 秒）转成 Unix epoch 秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _fetch(token: str, version: str) -> dict | None:
    """同步请求端点；任何网络/解析错误返回 None。"""
    req = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _OAUTH_BETA,
            "User-Agent": f"claude-code/{version}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except (urllib.error.URLError, ValueError, OSError):
        return None


def _read_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
    except OSError:
        pass


def get_usage(version: str | None = None) -> dict | None:
    """返回归一化的官方用量；无凭证/已禁用/无数据时返回 None。

    磁盘缓存：新鲜则直接复用；过期则尝试刷新一次（失败保留旧值并按
    `_RETRY_BACKOFF` 退避，避免每次 tick 都打一个挂掉的端点）。
    """
    if not _enabled():
        return None

    now = int(time.time())
    ttl = _ttl()
    cache = _read_cache()
    cached_data = cache.get("data")
    fetched_at = cache.get("fetched_at", 0)
    last_attempt = cache.get("last_attempt", 0)

    fresh = cached_data is not None and (now - fetched_at) < ttl
    backoff = (now - last_attempt) < _RETRY_BACKOFF

    if fresh or (cached_data is not None and backoff):
        return normalize(cached_data, cache.get("subscription_type"))

    token, sub_type = _read_token()
    if not token:
        return normalize(cached_data, sub_type) if cached_data is not None else None

    ua_version = (version or "").strip() or _FALLBACK_VERSION
    raw = _fetch(token, ua_version)
    if raw is not None:
        _write_cache({
            "fetched_at": now,
            "last_attempt": now,
            "subscription_type": sub_type,
            "data": raw,
        })
        return normalize(raw, sub_type)

    # 刷新失败：记录尝试时间用于退避，沿用旧值（若有）。
    cache["last_attempt"] = now
    _write_cache(cache)
    return normalize(cached_data, sub_type) if cached_data is not None else None


def _window(raw: dict, key: str, label: str) -> dict | None:
    w = raw.get(key)
    if not isinstance(w, dict):
        return None
    util = w.get("utilization")
    if util is None:
        return None
    return {
        "label": label,
        "utilization": float(util),
        "resets_at": _iso_to_epoch(w.get("resets_at")),
    }


def normalize(raw: dict | None, subscription_type: str | None = None) -> dict | None:
    """把端点原始响应归一化为渲染层友好的结构。

    返回::

        {
          "subscription_type": "enterprise" | "team" | ...,
          "windows": [ {"label": "5H", "utilization": 23.5, "resets_at": 1700}, ... ],
          "monthly": {  # 仅当 extra_usage 启用且有月度限额
             "used": 8176.0, "limit": 25000, "utilization": 32.7,
             "currency": "USD" | None,  # None 表示按 credits 计
          } | None,
        }

    全部为空时返回 None（无可显示内容）。
    """
    if not isinstance(raw, dict):
        return None

    windows: list[dict] = []
    for key, label in (("five_hour", "5H"), ("seven_day", "7D")):
        w = _window(raw, key, label)
        if w is not None:
            windows.append(w)

    monthly = None
    extra = raw.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") and extra.get("monthly_limit"):
        currency = extra.get("currency")
        monthly = {
            "used": float(extra.get("used_credits") or 0),
            "limit": float(extra.get("monthly_limit") or 0),
            "utilization": float(extra.get("utilization") or 0),
            # USD（含大小写）走美元预算显示，其余按 credits。
            "currency": "USD" if isinstance(currency, str) and currency.upper() == "USD" else None,
        }

    if not windows and monthly is None:
        return None

    return {
        "subscription_type": subscription_type,
        "windows": windows,
        "monthly": monthly,
    }
