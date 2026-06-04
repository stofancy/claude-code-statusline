"""JSONL transcript parsing for token metrics and replay estimation."""

import glob
import json
from pathlib import Path

# Cache: (path, mtime) → events to avoid re-reading on every tick
_cache: dict[str, tuple[float, list[dict]]] = {}

# Separate cache for token metrics (heavier computation, benefits from caching)
_metrics_cache: dict[str, tuple[float, dict]] = {}

# NEXT 投影时,即便没有增量数据也至少加上这个数,保证 NEXT ≥ 当前 context。
# 200 token ≈ 一个很短的 user 消息,避免显示成"零增长"。
MIN_NEXT_DELTA = 200


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

    去重策略：按 message.id 去重。同一 API 响应的 streaming chunk 和
    多 tool_use 拆分条目共享同一 message UUID，每个 UUID 只保留最后一条
    （含最终 usage 值）。此策略可正确处理 multi-tool-call 消息被
    tool_result 穿插写入 JSONL 的场景。

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
    # 计算 effective_mtime（主 transcript + 子代理中最新的 mtime），
    # 确保缓存检查使用与存储一致的 mtime，避免子代理存在时缓存总是 miss。
    sub_dir = p.parent / p.stem / "subagents"
    effective_mtime = mtime
    for f in glob.glob(str(sub_dir / "agent-*.jsonl")):
        try:
            effective_mtime = max(effective_mtime, Path(f).stat().st_mtime)
        except OSError:
            pass

    key = str(p)
    global _metrics_cache
    if key in _metrics_cache and _metrics_cache[key][0] == effective_mtime:
        return _metrics_cache[key][1]

    events = parse_transcript(transcript_path)
    if not events:
        metrics = _empty_metrics()
        _metrics_cache[key] = (effective_mtime, metrics)
        return metrics

    usage_entries = [e for e in events
                     if isinstance(e.get("message", {}).get("usage"), dict)]

    if not usage_entries:
        metrics = _empty_metrics()
        metrics["turn_count"] = sum(1 for e in events if e.get("type") == "user")
        _metrics_cache[key] = (effective_mtime, metrics)
        return metrics

    # 去重：按 message.id 去重，同一 UUID 保留最后一条。
    # 每个 API 调用有唯一 message UUID，streaming chunk 和
    # 被 tool_result 拆分的 multi-tool-call 条目共享同一 UUID。
    deduped = []
    seen_msg_ids: dict[str, int] = {}

    for e in events:
        usage = e.get("message", {}).get("usage")
        if not isinstance(usage, dict):
            continue

        msg_id = e.get("message", {}).get("id", "")

        if msg_id and msg_id in seen_msg_ids:
            deduped[seen_msg_ids[msg_id]] = e
            continue

        if msg_id:
            seen_msg_ids[msg_id] = len(deduped)
        deduped.append(e)

    # 找到最后一个真实 user 消息的时间戳,作为当前 turn 起点。
    # _extract_text 会过滤掉 tool_result(其 content 块是 tool_result 而非 text)。
    last_user_ts = ""
    for e in events:
        if e.get("type") == "user" and _extract_text(e):
            ts = e.get("timestamp", "")
            if ts and ts > last_user_ts:
                last_user_ts = ts

    # 聚合统计
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    model_usage: dict[str, dict] = {}
    most_recent_main = None
    most_recent_ts = ""
    max_context_in_turn = 0

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
        is_main = ts and not e.get("isSidechain") and not e.get("isApiErrorMessage")
        if is_main:
            if ts > most_recent_ts:
                most_recent_ts = ts
                most_recent_main = e
            # 当前 turn(晚于最后一个 user 消息)内的 main 调用取最大 context,
            # 防止单次 API 调用因 cache 命中比例波动把 CTX 百分比拉回。
            if last_user_ts and ts > last_user_ts:
                cl = it + cr + cw
                if cl > max_context_in_turn:
                    max_context_in_turn = cl

    # 优先使用当前 turn 的最大值;turn 内尚无新调用(用户刚发完消息,
    # 模型还没响应)时回落最近一次 main 调用
    if max_context_in_turn > 0:
        context_len = max_context_in_turn
    elif most_recent_main:
        u = most_recent_main.get("message", {}).get("usage", {})
        context_len = ((u.get("input_tokens", 0) or 0) +
                       (u.get("cache_read_input_tokens", 0) or 0) +
                       (u.get("cache_creation_input_tokens", 0) or 0))
    else:
        context_len = 0

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

    _metrics_cache[key] = (effective_mtime, metrics)
    return metrics


def _deduped_usage_events(events: list[dict]) -> list[dict]:
    """返回去重后的 usage 事件列表，按 message.id 去重，保留最后一条。"""
    deduped = []
    seen_msg_ids: dict[str, int] = {}
    for e in events:
        usage = e.get("message", {}).get("usage")
        if not isinstance(usage, dict):
            continue
        msg_id = e.get("message", {}).get("id", "")
        if msg_id and msg_id in seen_msg_ids:
            deduped[seen_msg_ids[msg_id]] = e
            continue
        if msg_id:
            seen_msg_ids[msg_id] = len(deduped)
        deduped.append(e)
    return deduped


def _context_growth_deltas(events: list[dict]) -> list[int]:
    """计算连续 API 调用间总上下文 token 的增量。

    每次 API 调用的总上下文 = input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens（三者互斥求和，匹配 Anthropic API usage 语义）。
    返回相邻调用间的正增量列表，取最近 10 个值。
    """
    deduped = _deduped_usage_events(events)
    total_lens = []
    for e in deduped:
        u = e.get("message", {}).get("usage", {})
        total = ((u.get("input_tokens", 0) or 0) +
                 (u.get("cache_read_input_tokens", 0) or 0) +
                 (u.get("cache_creation_input_tokens", 0) or 0))
        total_lens.append(total)

    deltas = []
    for i in range(1, len(total_lens)):
        d = total_lens[i] - total_lens[i - 1]
        if d > 0:
            deltas.append(d)
    return deltas[-10:]


def estimate_next_replay(
    transcript_path: str | None,
    latest_call_input: int = 0,
    total_input_tokens: int = 0,
    ctx_window_size: int = 1_000_000,
    current_context_len: int = 0,
) -> dict:
    events = parse_transcript(transcript_path)
    recent = recent_turn_sizes(events, n=5) if events else []
    trend = detect_growth_trend(recent)
    turns = count_turns(events) if events else 0
    avg_turn = sum(recent) // max(len(recent), 1) if recent else 0

    if current_context_len > 0 and events:
        deltas = _context_growth_deltas(events)
        avg_growth = sum(deltas) // max(len(deltas), 1) if deltas else 0
        # NEXT = 当前 context + 平均增量(典型新内容)。不要用 latest_call_input
        # —— 那是上一次调用的绝对大小,已经被包含在 current_context_len 里,
        # 再加一遍会得到 ~2× 关系。MIN_NEXT_DELTA 保证 NEXT 至少略高于当前。
        growth = max(avg_growth, MIN_NEXT_DELTA)
        projected = current_context_len + growth
    elif latest_call_input > 0:
        projected = latest_call_input
    elif total_input_tokens > 0:
        projected = total_input_tokens
    else:
        ce = content_events(events) if events else []
        total = sum(_estimate_tokens(_extract_text(ev)) for ev in ce)
        projected = total + avg_turn

    return {"estimated_tokens": projected, "growth_trend": trend, "turn_count": turns}
