"""Codex rollout JSONL parsing for a Claude-style status snapshot.

Codex does not expose Claude Code's ``statusLine.command`` protocol. The
closest proven approach, used by existing community tooling, is a ``Stop`` hook
that appends a small "faux statusline" after each assistant turn.  Codex writes
authoritative token snapshots as ``event_msg`` / ``token_count`` records, so we
prefer those over ad-hoc response item parsing.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Any

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
SESSIONS_DIR = CODEX_HOME / "sessions"
MIN_NEXT_DELTA = 200


def _zero_usage() -> dict[str, int]:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "context_len": 0}


def parse_rollout(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events


def latest_rollout(sessions_dir: str | Path | None = None) -> Path | None:
    root = Path(sessions_dir) if sessions_dir else SESSIONS_DIR
    candidates = []
    for name in glob.glob(str(root / "**" / "rollout-*.jsonl"), recursive=True):
        p = Path(name)
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _usage_from_payload(payload: dict[str, Any]) -> dict[str, int] | None:
    """Extract OpenAI/Responses-style token usage from a Codex payload."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        response = payload.get("response")
        if isinstance(response, dict):
            usage = response.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    details = usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    cache_read = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)

    non_cache_input = max(0, input_tokens - cache_read)
    return {
        "input": non_cache_input,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_write": 0,
        "context_len": input_tokens,
    }


def _usage_from_token_count(info: dict[str, Any], key: str) -> dict[str, int]:
    usage = info.get(key) or {}
    if not isinstance(usage, dict):
        return _zero_usage()
    input_tokens = int(usage.get("input_tokens") or 0)
    cached = int(usage.get("cached_input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0) + int(usage.get("reasoning_output_tokens") or 0)
    return {
        "input": max(0, input_tokens - cached),
        "output": output_tokens,
        "cache_read": cached,
        "cache_write": 0,
        "context_len": input_tokens,
    }


def _rate_limit_windows(rate_limits: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(rate_limits, dict):
        return None
    windows = []
    for bucket in (rate_limits.get("primary"), rate_limits.get("secondary")):
        if not isinstance(bucket, dict):
            continue
        pct = bucket.get("used_percent")
        if pct is None:
            continue
        window = int(bucket.get("window_minutes") or 0)
        label = "5H" if window and window < 1440 else "7D"
        windows.append({"label": label, "utilization": float(pct), "resets_at": bucket.get("resets_at")})
    if not windows:
        return None
    windows.sort(key=lambda w: 0 if w["label"] == "5H" else 1)
    return {"subscription_type": rate_limits.get("plan_type"), "windows": windows, "monthly": None}


def _model_from_payload(payload: dict[str, Any], fallback: str) -> str:
    for key in ("model", "model_slug", "model_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    response = payload.get("response")
    if isinstance(response, dict):
        value = response.get("model")
        if isinstance(value, str) and value:
            return value
    return fallback or "unknown"


def _dedupe_key(event: dict[str, Any], usage: dict[str, int]) -> tuple[Any, ...]:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    response = payload.get("response") or {}
    if not isinstance(response, dict):
        response = {}
    rid = payload.get("id") or payload.get("response_id") or response.get("id")
    if rid:
        return ("id", rid)
    return (
        "usage",
        event.get("timestamp"),
        usage["input"],
        usage["output"],
        usage["cache_read"],
        usage["cache_write"],
    )


def session_metrics(path: str | Path | None) -> dict[str, Any]:
    events = parse_rollout(path)
    model = "unknown"
    session_id = ""
    started_at = ""
    last_ts = ""
    turn_count = 0
    tool_count = 0
    compactions = 0
    effort = ""
    cwd = ""
    ctx_window = 0
    official_usage = None
    token_total: dict[str, int] | None = None
    token_last: dict[str, int] | None = None
    token_context_lens: list[int] = []
    usage_items: list[tuple[dict[str, Any], dict[str, int], str]] = []
    seen: dict[tuple[Any, ...], int] = {}

    for event in events:
        etype = event.get("type")
        ts = event.get("timestamp") or ""
        if ts:
            started_at = started_at or ts
            last_ts = ts
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        if etype == "session_meta":
            session_id = str(payload.get("id") or session_id)
            cwd = str(payload.get("cwd") or cwd)
            model = _model_from_payload(payload, model)
        elif etype == "turn_context":
            turn_count += 1
            model = _model_from_payload(payload, model)
            effort = str(payload.get("effort") or effort)
            cwd = str(payload.get("cwd") or cwd)
        elif etype == "event_msg":
            msg_type = str(payload.get("type") or "")
            if msg_type in {"exec_command_begin", "tool_call_begin"}:
                tool_count += 1
            if "compact" in msg_type:
                compactions += 1
            if msg_type == "token_count":
                info = payload.get("info") or {}
                if isinstance(info, dict):
                    token_total = _usage_from_token_count(info, "total_token_usage")
                    token_last = _usage_from_token_count(info, "last_token_usage")
                    if token_last["context_len"]:
                        token_context_lens.append(token_last["context_len"])
                    ctx_window = int(info.get("model_context_window") or ctx_window or 0)
                official_usage = _rate_limit_windows(payload.get("rate_limits")) or official_usage

        usage = _usage_from_payload(payload)
        if usage is None:
            continue
        mid = _model_from_payload(payload, model)
        key = _dedupe_key(event, usage)
        if key in seen:
            usage_items[seen[key]] = (event, usage, mid)
        else:
            seen[key] = len(usage_items)
            usage_items.append((event, usage, mid))

    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    model_usage: dict[str, dict[str, int]] = {}
    context_len = 0
    last_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "context_len": 0}
    context_lens: list[int] = []

    for _event, usage, mid in usage_items:
        for key in totals:
            totals[key] += usage[key]
        if mid not in model_usage:
            model_usage[mid] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        for key in model_usage[mid]:
            model_usage[mid][key] += usage[key]
        context_len = max(context_len, usage["context_len"])
        context_lens.append(usage["context_len"])
        last_usage = usage
        model = mid or model

    if token_total is not None:
        for key in totals:
            totals[key] = token_total[key]
        context_len = (token_last or token_total)["context_len"]
        last_usage = token_last or token_total
        model_usage = {model: dict(totals)} if model else {}
        context_lens = token_context_lens

    positive_deltas = [b - a for a, b in zip(context_lens, context_lens[1:]) if b > a]
    avg_delta = sum(positive_deltas[-10:]) // max(1, len(positive_deltas[-10:])) if positive_deltas else MIN_NEXT_DELTA
    replay_tokens = (context_lens[-1] + max(avg_delta, MIN_NEXT_DELTA)) if context_lens else 0

    return {
        "path": str(path or ""),
        "session_id": session_id,
        "model_id": model,
        "effort": effort,
        "cwd": cwd,
        "started_at": started_at,
        "last_timestamp": last_ts,
        "turn_count": turn_count,
        "tool_call_count": tool_count,
        "compaction_count": compactions,
        "input": totals["input"],
        "output": totals["output"],
        "cache_read": totals["cache_read"],
        "cache_write": totals["cache_write"],
        "context_len": context_len,
        "context_window_size": ctx_window,
        "replay_tokens": replay_tokens,
        "last_usage": last_usage,
        "model_usage": model_usage,
        "official_usage": official_usage,
    }
