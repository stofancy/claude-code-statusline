"""JSONL transcript parsing for replay token estimation and conversation analysis."""

import json
from pathlib import Path

# Cache: (path, mtime) → events to avoid re-reading on every tick
_cache: dict[str, tuple[float, list[dict]]] = {}


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
        projected = latest_call_input + avg_turn
    elif total_input_tokens > 0:
        projected = total_input_tokens
    else:
        ce = content_events(events) if events else []
        total = sum(_estimate_tokens(_extract_text(ev)) for ev in ce)
        projected = total + avg_turn

    return {"estimated_tokens": projected, "growth_trend": trend, "turn_count": turns}
