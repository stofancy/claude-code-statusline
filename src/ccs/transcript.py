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


def _context_total(usage: dict) -> int:
    """计算一次 API 调用的总上下文 token 数。

    Anthropic API（及多数提供商）中 input_tokens、cache_read_input_tokens、
    cache_creation_input_tokens 三者互斥求和。此函数包装该求和逻辑避免各处手写。
    """
    return ((usage.get("input_tokens", 0) or 0) +
            (usage.get("cache_read_input_tokens", 0) or 0) +
            (usage.get("cache_creation_input_tokens", 0) or 0))


def _looks_like_cache_rebuild(usage: dict) -> bool:
    """usage 是否像 prompt-cache 整段重建（非稳态上下文窗口大小）。

    特征：token 主体是 cache_creation；非缓存 input 极小；允许少量 residual
    cache_read（旧启发式要求 cr==0，会被 cr=1000 的变种穿透）。
    """
    total = _context_total(usage)
    if total <= 0:
        return False
    it = usage.get("input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    if cw <= 0:
        return False
    # 主体应是 cache write（≥80%）
    if cw * 5 < total * 4:
        return False
    # residual cache_read 不得超过 5% 或 2k
    if cr > max(2000, total // 20):
        return False
    # 非缓存 input 只应是骨架
    if it > max(200, total // 50):
        return False
    return True


def _is_transient_context_spike(
    prev_total: int,
    cur_total: int,
    next_total: int | None,
    usage: dict,
) -> bool:
    """判断 cur 是否为短暂抬高 context_len 的尖峰，而非真实窗口增长。

    优先用邻域共识：相对 prev 显著抬升，且 next 回到 prev 附近 → 尖峰。
    无 next（当前 tip）时，仅在 cache-rebuild 形态 + 大幅抬升时判定为尖峰，
    避免把真实的大附件/长上下文增长误杀。
    """
    if prev_total <= 0 or cur_total <= prev_total:
        return False

    jumped = cur_total >= int(prev_total * 1.5) or (cur_total - prev_total) >= 50_000
    if not jumped:
        return False

    if next_total is not None and next_total > 0:
        # next 回到 prev 一带（15% 或 5k 容差）→ 明确的尖峰
        band = max(int(prev_total * 0.15), 5_000)
        if abs(next_total - prev_total) <= band:
            return True
        # next 远小于 cur，且比 cur 更接近 prev
        if (next_total * 10 < cur_total * 7 and
                abs(next_total - prev_total) < abs(cur_total - prev_total)):
            return True
        return False

    # tip：没有后继佐证，只过滤 cache-rebuild 形态的大幅抬升
    return _looks_like_cache_rebuild(usage)


def _pick_stable_context_len(usages: list[dict]) -> int:
    """从按时间排序的 usage 序列中选取最近一次非尖峰上下文长度。"""
    if not usages:
        return 0
    totals = [_context_total(u) for u in usages]
    n = len(totals)
    for i in range(n - 1, -1, -1):
        prev_t = totals[i - 1] if i > 0 else 0
        next_t = totals[i + 1] if i + 1 < n else None
        if i > 0 and _is_transient_context_spike(prev_t, totals[i], next_t, usages[i]):
            continue
        return totals[i]
    return totals[-1]


def _stable_context_totals(usages: list[dict]) -> list[int]:
    """去掉短暂尖峰后的上下文长度序列，供 NEXT 增长估算使用。"""
    if not usages:
        return []
    totals = [_context_total(u) for u in usages]
    out: list[int] = []
    for i, t in enumerate(totals):
        prev_t = totals[i - 1] if i > 0 else 0
        next_t = totals[i + 1] if i + 1 < len(totals) else None
        if i > 0 and _is_transient_context_spike(prev_t, t, next_t, usages[i]):
            continue
        out.append(t)
    return out


def _estimate_tokens(text: str) -> int:
    """估算文本 token 数。英文 ~4 char/token，CJK ~1.5 char/token。
    用 Unicode 范围检测混合文本比例，加权取平均字符/token 比率。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿' or
              '぀' <= ch <= 'ヿ' or '가' <= ch <= '힯')
    cjk_ratio = cjk / len(text) if text else 0
    chars_per_token = (3.5 * (1 - cjk_ratio)) + (1.5 * cjk_ratio)
    return max(1, int(len(text) / chars_per_token))


def _extract_text(ev: dict) -> str:
    """提取消息中的纯文本内容（不含 tool_use/tool_result）。

    此函数用于 turn 边界检测——tool_result 虽然 type=user 但不代表
    新 turn 起点，必须通过此函数过滤掉。
    只收集非空 text；多个无 text 块不得拼出 "\\n" 这种假阳性。
    """
    msg = ev.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                if isinstance(block, str) and block:
                    parts.append(block)
                continue
            btype = block.get("type")
            if btype in ("tool_result", "tool_use", "image", "document", "thinking"):
                continue
            text = block.get("text") or ""
            if text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _extract_all_text(ev: dict) -> str:
    """提取消息中所有文本（含 tool_use 和 tool_result 块），用于 token 估算。

    tool_use 消息可能没有 text 块（只有 tool name+input），tool_result
    消息包含大量输出内容——这些都应纳入 turn size 估算。
    """
    msg = ev.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = json.dumps(block.get("input", {}), ensure_ascii=False)
                parts.append(f"{name} {inp}" if name else inp)
            elif block.get("type") == "tool_result":
                tc = block.get("content", "")
                if isinstance(tc, str):
                    parts.append(tc)
                elif isinstance(tc, list):
                    parts.extend(str(c) for c in tc)
            else:
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
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


def _is_human_user_turn(ev: dict) -> bool:
    """是否为真人对话轮次（排除 tool_result / 斜杠命令 / 本地命令回显 / task 通知）。"""
    if ev.get("type") != "user":
        return False
    text = _extract_text(ev)
    if not text or not str(text).strip():
        return False
    s = str(text).lstrip()
    if s.startswith("<command-name>") or s.startswith("<command-message>"):
        return False
    if s.startswith("<local-command-") or s.startswith("<local-command"):
        return False
    if s.startswith("<task-notification"):
        return False
    return True


def content_events(events: list[dict]) -> list[dict]:
    return [ev for ev in events if ev.get("type") in ("user", "assistant") and _extract_text(ev)]


def count_turns(events: list[dict]) -> int:
    return sum(1 for ev in events if _is_human_user_turn(ev))


def recent_turn_sizes(events: list[dict], n: int = 5) -> list[int]:
    """估算最近几轮对话的 token 大小（含 tool_use 和 tool_result 内容）。

    turn 边界仅由 _extract_text 识别（排除 tool_result），但每轮内所有
    user/assistant 消息（含 tool_use/tool_result）都纳入 token 估算。
    """
    sizes = []
    cur = 0
    for ev in events:
        ev_type = ev.get("type", "")
        if ev_type not in ("user", "assistant"):
            continue
        # turn 边界: 真人文本 user（排除 tool_result / 斜杠命令回显）
        if ev_type == "user" and _is_human_user_turn(ev) and cur > 0:
            sizes.append(cur)
            cur = 0
        # 用 _extract_all_text 估算所有消息（含 tool_use/tool_result）的大小
        cur += _estimate_tokens(_extract_all_text(ev))
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
            "turn_count": 0, "context_len": 0, "compaction_count": 0,
            "model_usage": {}, "model_calls": {}}


def _merge_metrics(dest: dict, src: dict) -> None:
    """将 *src* metrics 合并到 *dest*（原地修改）。

    合并 token 与 compaction；不合并 turn_count / context_len——
    子代理内部轮次不是主会话对话轮次，子代理上下文窗口也独立。
    """
    for k in ("input", "output", "cache_read", "cache_write", "compaction_count"):
        if k in src:
            dest[k] = dest.get(k, 0) + src[k]
    for model_id, usage in src.get("model_usage", {}).items():
        if model_id not in dest["model_usage"]:
            dest["model_usage"][model_id] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        for tk in ("input", "output", "cache_read", "cache_write"):
            dest["model_usage"][model_id][tk] += usage.get(tk, 0)
    # 逐调用明细（含时间戳）同样合并，供潮汐定价按调用时间精算
    for model_id, calls in src.get("model_calls", {}).items():
        dest.setdefault("model_calls", {}).setdefault(model_id, [])
        dest["model_calls"][model_id].extend(calls)


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

    key = str(p)
    global _metrics_cache

    # 缓存键必须纳入子代理 mtime：仅用主文件 mtime 会在子代理单独更新时
    # 命中陈旧缓存，漏加 subagent token。
    sub_dir = p.parent / p.stem / "subagents"
    effective_mtime = mtime
    if sub_dir.exists():
        for f in glob.glob(str(sub_dir / "agent-*.jsonl")):
            try:
                effective_mtime = max(effective_mtime, Path(f).stat().st_mtime)
            except OSError:
                pass

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

    # 聚合统计
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    model_usage: dict[str, dict] = {}
    model_calls: dict[str, list] = {}
    # (timestamp, usage)；随后按 timestamp 排序，避免文件顺序/去重替换位与时间分叉
    main_calls: list[tuple[str, dict]] = []

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
        # 逐调用明细：(ts, usage)——供潮汐定价按每次调用时间精确计价
        if model not in model_calls:
            model_calls[model] = []
        model_calls[model].append((ts or "", {
            "input": it, "output": ot, "cache_read": cr, "cache_write": cw}))

        is_main = not e.get("isSidechain") and not e.get("isApiErrorMessage")
        if is_main:
            # 无 timestamp 时用空串；排序后仍保留，只是排在最前
            main_calls.append((ts or "", usage))

    main_calls.sort(key=lambda item: item[0])
    main_usages = [u for _, u in main_calls]
    # context_len：最近一次非短暂尖峰的 main 调用上下文。
    # 不用 turn 内 max——agentic 工具环很长，真人消息可能在数十分钟前，
    # max 会把一次 cache 重建尖峰锁到整段循环结束。
    context_len = _pick_stable_context_len(main_usages)

    # 对话轮次 = 带真实文本的 user 消息；不含 tool_result 事件
    turn_count = count_turns(events)
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
        "model_calls": model_calls,
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


def _main_usages_from_events(events: list[dict]) -> list[dict]:
    """去重后的 main（非 sidechain / 非 API error）usage，按 timestamp 排序。"""
    mains: list[tuple[str, dict]] = []
    for e in _deduped_usage_events(events):
        if e.get("isSidechain") or e.get("isApiErrorMessage"):
            continue
        u = e.get("message", {}).get("usage", {})
        if not isinstance(u, dict):
            continue
        mains.append((e.get("timestamp") or "", u))
    mains.sort(key=lambda item: item[0])
    return [u for _, u in mains]


def _context_growth_deltas(events: list[dict]) -> list[int]:
    """计算连续 API 调用间总上下文 token 的增量。

    使用与 context_len 相同的尖峰过滤，避免 230k→567k 一次性抬升进入平均增长。
    另过滤 >50% 的跳变（压缩恢复 / 计量字段切换），取最近 10 个正增量。
    """
    total_lens = _stable_context_totals(_main_usages_from_events(events))

    deltas = []
    for i in range(1, len(total_lens)):
        d = total_lens[i] - total_lens[i - 1]
        if d <= 0:
            continue
        prev = max(total_lens[i - 1], 1)
        if d * 2 > prev:
            continue
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
