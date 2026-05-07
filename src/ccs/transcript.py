"""JSONL transcript parsing for token metrics and replay estimation."""

import glob
import json
from pathlib import Path

# Cache: (path, mtime) → events to avoid re-reading on every tick
_cache: dict[str, tuple[float, list[dict]]] = {}

# Separate cache for token metrics (heavier computation, benefits from caching)
_metrics_cache: dict[str, tuple[float, dict]] = {}
_metrics_cache_path: str = ""


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.5))


def _extract_text(ev: dict) -> str:
    msg = ev.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") if isinstance(block, dict) else str(block)
                 for block in content]
        return "\n".join(parts)
    return ""


def parse_transcript(path: str | None) -> list[dict]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    mtime = p.stat().st_mtime
    key = str(p)
    if key in _cache and _cache[key][0] == mtime:
        return _cache[key][1]
    events = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (OSError, PermissionError):
        return []
    _cache[key] = (mtime, events)
    return events


def content_events(events: list[dict]) -> list[dict]:
    return [ev for ev in events if ev.get("type") in ("user", "assistant") and _extract_text(ev)]


def count_turns(events: list[dict]) -> int:
    return sum(1 for ev in events if ev.get("type") == "user" and _extract_text(ev))


def recent_turn_sizes(events: list[dict], n: int = 5) -> list[int]:
    ce = content_events(events)
    sizes = []
    cur = 0
    for ev in ce:
        if ev.get("type") == "user" and cur > 0:
            sizes.append(cur)
            cur = 0
        cur += _estimate_tokens(_extract_text(ev))
    if cur > 0:
        sizes.append(cur)
    return sizes[-n:] if len(sizes) > n else sizes


def detect_growth_trend(recent_sizes: list[int]) -> str:
    if len(recent_sizes) < 3:
        return "stable"
    mid = len(recent_sizes) // 2
    first = sum(recent_sizes[:mid]) / max(mid, 1)
    second = sum(recent_sizes[mid:]) / max(len(recent_sizes) - mid, 1)
    if first == 0:
        return "rising" if second > 0 else "stable"
    change = (second - first) / first
    if change > 0.15:
        return "rising"
    elif change < -0.15:
        return "declining"
    return "stable"


def _empty_metrics() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
            "turn_count": 0, "context_len": 0, "compaction_count": 0, "model_usage": {}}


def _merge_metrics(dest: dict, src: dict) -> None:
    """将 *src* metrics 合并到 *dest*（原地修改）。"""
    for k in ("input", "output", "cache_read", "cache_write",
              "turn_count", "compaction_count"):
        if k in src:
            dest[k] = dest.get(k, 0) + src[k]
    # context_len 保持主 transcript 的值，子代理上下文独立
    for model_id, usage in src.get("model_usage", {}).items():
        if model_id not in dest["model_usage"]:
            dest["model_usage"][model_id] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        for tk in ("input", "output", "cache_read", "cache_write"):
            dest["model_usage"][model_id][tk] += usage.get(tk, 0)


def _subagent_metrics(transcript_path: str) -> dict:
    """解析 *transcript_path* 对应 session 的所有子代理 transcript。"""
    p = Path(transcript_path)
    sub_dir = p.parent / p.stem / "subagents"
    if not sub_dir.exists():
        return _empty_metrics()

    merged = _empty_metrics()
    for agent_jsonl in glob.glob(str(sub_dir / "agent-*.jsonl")):
        fm = get_session_metrics(agent_jsonl)
        _merge_metrics(merged, fm)
    return merged


def get_session_metrics(transcript_path: str) -> dict:
    """从 JSONL transcript 解析精确的 session token 统计。

    去重策略：连续条目 usage 四元组相同 → 只保留最后一条。
    按 model 字段分组统计，支持多模型会话。

    Returns:
        {input, output, cache_read, cache_write, turn_count, context_len, model_usage}
        异常或路径不可用时返回全零值。
    """
    if not transcript_path:
        return _empty_metrics()

    p = Path(transcript_path)
    if not p.exists():
        return _empty_metrics()

    mtime = p.stat().st_mtime
    key = str(p)
    global _metrics_cache
    if key in _metrics_cache and _metrics_cache[key][0] == mtime:
        return _metrics_cache[key][1]

    events = parse_transcript(transcript_path)
    if not events:
        metrics = _empty_metrics()
        _metrics_cache[key] = (mtime, metrics)
        return metrics

    usage_entries = [e for e in events
                     if isinstance(e.get("message", {}).get("usage"), dict)]

    if not usage_entries:
        metrics = _empty_metrics()
        metrics["turn_count"] = sum(1 for e in events if e.get("type") == "user")
        _metrics_cache[key] = (mtime, metrics)
        return metrics

    # 去重：只在同一 turn 内连续相同 usage 的条目间去重
    # 关键：user 消息打断连续链，防止跨 turn 误去重
    deduped = []
    prev_usage_tuple = None
    seen_user_since_last_usage = True

    for e in events:
        if e.get("type") == "user":
            seen_user_since_last_usage = True
            continue

        usage = e.get("message", {}).get("usage")
        if not isinstance(usage, dict):
            continue

        curr = (usage.get("input_tokens"), usage.get("output_tokens"),
                usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens"))

        if not seen_user_since_last_usage and curr == prev_usage_tuple:
            deduped[-1] = e  # 替换为同组最后一条
            prev_usage_tuple = curr
            continue

        seen_user_since_last_usage = False
        prev_usage_tuple = curr
        deduped.append(e)

    # 聚合统计
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    model_usage: dict[str, dict] = {}
    most_recent_main = None
    most_recent_ts = ""

    for e in deduped:
        usage = e.get("message", {}).get("usage", {})
        model = e.get("message", {}).get("model", "unknown")

        it = usage.get("input_tokens", 0) or 0
        ot = usage.get("output_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        cw = usage.get("cache_creation_input_tokens", 0) or 0

        input_tokens += it
        output_tokens += ot
        cache_read += cr
        cache_write += cw

        if model not in model_usage:
            model_usage[model] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        model_usage[model]["input"] += it
        model_usage[model]["output"] += ot
        model_usage[model]["cache_read"] += cr
        model_usage[model]["cache_write"] += cw

        ts = e.get("timestamp", "")
        if ts and not e.get("isSidechain") and not e.get("isApiErrorMessage"):
            if ts > most_recent_ts:
                most_recent_ts = ts
                most_recent_main = e

    context_len = 0
    if most_recent_main:
        u = most_recent_main.get("message", {}).get("usage", {})
        context_len = ((u.get("input_tokens", 0) or 0) +
                       (u.get("cache_read_input_tokens", 0) or 0) +
                       (u.get("cache_creation_input_tokens", 0) or 0))

    turn_count = sum(1 for e in events if e.get("type") == "user")
    compaction_count = sum(1 for e in events
                           if e.get("type") == "system" and e.get("subtype") == "compact_boundary")

    metrics = {
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "turn_count": turn_count,
        "context_len": context_len,
        "compaction_count": compaction_count,
        "model_usage": model_usage,
    }

    # 聚合子代理 transcript（agent-*.jsonl）
    sub = _subagent_metrics(transcript_path)
    _merge_metrics(metrics, sub)

    # 缓存 key 取主 transcript + 子代理中最新的 mtime
    sub_dir = p.parent / p.stem / "subagents"
    effective_mtime = mtime
    for f in glob.glob(str(sub_dir / "agent-*.jsonl")):
        try:
            effective_mtime = max(effective_mtime, Path(f).stat().st_mtime)
        except OSError:
            pass

    _metrics_cache[key] = (effective_mtime, metrics)
    return metrics


def estimate_next_replay(
    transcript_path: str | None,
    latest_call_input: int = 0,
    total_input_tokens: int = 0,
    ctx_window_size: int = 1_000_000,
) -> dict:
    events = parse_transcript(transcript_path)
    recent = recent_turn_sizes(events, n=5) if events else []
    trend = detect_growth_trend(recent)
    turns = count_turns(events) if events else 0
    avg_turn = sum(recent) // max(len(recent), 1) if recent else 0

    if latest_call_input > 0:
        projected = total_input_tokens + latest_call_input
    elif total_input_tokens > 0:
        projected = total_input_tokens
    else:
        ce = content_events(events) if events else []
        total = sum(_estimate_tokens(_extract_text(ev)) for ev in ce)
        projected = total + avg_turn

    return {"estimated_tokens": projected, "growth_trend": trend, "turn_count": turns}
