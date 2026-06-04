"""Statusline display CLI. Called by Claude Code on every statusline tick."""

import json
import sys
import time

from . import db
from . import cost as cost_mod
from . import transcript as tx_mod
from . import renderer
from . import usage as usage_mod
from .i18n import t
from .util import read_stdin_json


def _is_claude_model(model_id: str) -> bool:
    """判断当前模型是否为 Claude 官方模型（支持 /api/oauth/usage 用量查询）。

    当前仅 Claude 官方模型实现了用量查询（5H/7D 窗口 + 月度预算）。
    DeepSeek、OpenAI、Gemini 等其他提供商暂不支持，不显示官方用量段。
    非官方/代理模型（即使底层用 Claude）也不走此路径。
    """
    if not model_id or model_id == "unknown":
        return False
    return "claude" in model_id.lower()


def _stdin_windows(rate_limits: dict) -> list[dict]:
    """从 stdin rate_limits 构造滚动窗口（仅 Pro/Max 提供，resets_at 为 epoch 秒）。"""
    windows = []
    for key, label in (("five_hour", "5H"), ("seven_day", "7D")):
        w = rate_limits.get(key)
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percentage")
        if pct is None:
            continue
        windows.append({
            "label": label,
            "utilization": float(pct),
            "resets_at": w.get("resets_at"),
        })
    return windows


def _resolve_official_usage(version, rate_limits: dict) -> dict | None:
    """合并 API 与 stdin 两路官方用量；逐窗口以 API 为准、stdin 兜底。"""
    try:
        api = usage_mod.get_usage(version)
    except Exception:
        api = None

    stdin_windows = _stdin_windows(rate_limits or {})

    if api is None:
        if not stdin_windows:
            return None
        return {"subscription_type": None, "windows": stdin_windows, "monthly": None}

    # API 窗口优先，缺失的窗口用 stdin 补齐（保持 5H→7D 顺序）。
    have = {w["label"] for w in api.get("windows", [])}
    merged = list(api.get("windows", []))
    for w in stdin_windows:
        if w["label"] not in have:
            merged.append(w)
    merged.sort(key=lambda w: 0 if w["label"] == "5H" else 1)
    api["windows"] = merged
    return api


def main() -> None:
    data = read_stdin_json()

    if not data:
        print(f"\033[2m{t('WAITING')}\033[0m")
        sys.exit(0)

    try:
        db.init_db()
    except Exception:
        pass

    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    model = data.get("model", {})
    model_id = model.get("id", "unknown") if isinstance(model, dict) else "unknown"
    model_name = model.get("display_name", "") if isinstance(model, dict) else ""
    if not model_name or model_name == model_id:
        model_name = model_id.replace("[1m]", "").replace("-", " ").title()
    cost_data = data.get("cost", {})
    ctx = data.get("context_window", {})

    # 官方订阅用量：仅 Claude 官方模型支持（/api/oauth/usage 端点），
    # 非 Claude 模型（DeepSeek、OpenAI、Gemini 等）暂不显示。
    official_usage = None
    if _is_claude_model(model_id):
        rate_limits = data.get("rate_limits") or {}
        official_usage = _resolve_official_usage(data.get("version"), rate_limits)

    cur_snapshot_input = ctx.get("total_input_tokens", 0) or 0
    snapshot_pct = ctx.get("used_percentage")
    ctx_size = ctx.get("context_window_size", 1_000_000) or 1_000_000

    cu = ctx.get("current_usage") or {}
    pc_input = cu.get("input_tokens", 0) or 0
    pc_output = cu.get("output_tokens", 0) or 0
    pc_cache_read = cu.get("cache_read_input_tokens", 0) or 0
    pc_cache_write = cu.get("cache_creation_input_tokens", 0) or 0

    per_call_total_input = pc_input + pc_cache_read + pc_cache_write

    try:
        db.ensure_session(session_id, model_id, model_name)
        metrics = tx_mod.get_session_metrics(transcript_path)
        db.update_session_tokens(session_id, metrics)
        db.update_model_usage(session_id, metrics.get("model_usage", {}))
        compaction_count = metrics.get("compaction_count", 0)
    except Exception:
        metrics = {}
        compaction_count = 0

    # CTX: 优先使用 transcript 解析的 context_len，fallback 到 snapshot
    tx_context_len = metrics.get("context_len", 0)
    if tx_context_len > 0:
        ctx_pct = tx_context_len / ctx_size * 100
    else:
        ctx_pct = snapshot_pct

    # Replay estimation: 用 transcript context_len 替代 snapshot total_input_tokens
    try:
        replay_info = tx_mod.estimate_next_replay(
            transcript_path,
            latest_call_input=per_call_total_input,
            total_input_tokens=cur_snapshot_input,
            ctx_window_size=ctx_size,
            current_context_len=tx_context_len,
        )
        replay_tokens = replay_info.get("estimated_tokens", 0)
        transcript_turns = replay_info.get("turn_count", 0)
    except Exception:
        replay_tokens = per_call_total_input or tx_context_len or 0
        transcript_turns = 0

    session = {}
    try:
        session = db.get_session(session_id) or {}
    except Exception:
        pass

    db_turns = session.get("turn_count") or 0
    turn_count = db_turns if db_turns > 0 else transcript_turns
    session_start_ts = session.get("started_at")
    if not session_start_ts:
        dur_ms = cost_data.get("total_duration_ms", 0) if isinstance(cost_data, dict) else 0
        if dur_ms:
            session_start_ts = int(time.time()) - int(dur_ms / 1000)

    # Aggregate ALL sessions (main + subagents) for token/cost totals
    agg = db.get_all_totals(session_id)
    cum_input = agg["tot_input_tokens"]
    cum_output = agg["tot_output_tokens"]
    cum_cache_total = agg["tot_cache_read_tokens"]
    cum_cache_write_total = agg["tot_cache_write_tokens"]
    tool_call_count = agg["tool_call_count"]
    subagent_total = agg["subagent_total"]
    subagent_running = agg["subagent_running"]

    try:
        cost_str = cost_mod.fmt_cost_multi(
            db.get_model_breakdown(session_id),
            primary_model_id=model_id or None,
        )
    except Exception:
        cc_cost = cost_data.get("total_cost_usd", 0) if isinstance(cost_data, dict) else 0
        cost_str = f"${cc_cost:.2f}" if cc_cost else "-"

    try:
        last_cost_str = cost_mod.fmt_last_cost(model_id, pc_input, pc_output, pc_cache_read, pc_cache_write)
    except Exception:
        last_cost_str = "-"

    try:
        pred_output = int(pc_output * 1.25)
        pc_total = max(per_call_total_input, 1)
        pred_cache = int(replay_tokens * pc_cache_read // pc_total)
        pred_cache_write = int(replay_tokens * pc_cache_write // pc_total)
        pred_input = max(0, replay_tokens - pred_cache - pred_cache_write)
        pred_cost_str = cost_mod.fmt_last_cost(model_id, pred_input, pred_output, pred_cache, pred_cache_write)
    except Exception:
        pred_cost_str = "-"

    try:
        output = renderer.render(
            model_name=model_name,
            ctx_pct=ctx_pct,
            replay_tokens=replay_tokens,
            total_input=cum_input,
            total_output=cum_output,
            cache_read=cum_cache_total,
            cache_write=cum_cache_write_total,
            cost_str=cost_str,
            last_cost_str=last_cost_str,
            pred_cost_str=pred_cost_str,
            pred_output=pred_output if pred_cost_str != "-" else 0,
            session_start_ts=session_start_ts,
            turn_count=turn_count,
            tool_call_count=tool_call_count,
            subagent_total=subagent_total,
            subagent_running=subagent_running,
            compaction_count=compaction_count,
            ctx_window_size=ctx_size,
            official_usage=official_usage,
        )
        print(output)
    except Exception as exc:
        print(f"\033[31mccs error: {exc}\033[0m", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
