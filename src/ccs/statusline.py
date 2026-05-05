"""Statusline display CLI. Called by Claude Code on every statusline tick."""

import json
import sys
import time

from . import db
from . import cost as cost_mod
from . import transcript as tx_mod
from . import renderer
from .util import read_stdin_json


def main() -> None:
    data = read_stdin_json()

    if not data:
        print("\033[2mccs: waiting for session data...\033[0m")
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

    cur_snapshot_input = ctx.get("total_input_tokens", 0) or 0
    cur_snapshot_output = ctx.get("total_output_tokens", 0) or 0
    ctx_pct = ctx.get("used_percentage")
    ctx_size = ctx.get("context_window_size", 1_000_000) or 1_000_000

    cu = ctx.get("current_usage") or {}
    pc_input = cu.get("input_tokens", 0) or 0
    pc_output = cu.get("output_tokens", 0) or 0
    pc_cache_read = cu.get("cache_read_input_tokens", 0) or 0
    pc_cache_write = cu.get("cache_creation_input_tokens", 0) or 0

    per_call_total_input = pc_input + pc_cache_read + pc_cache_write

    try:
        db.ensure_session(session_id, model_id, model_name)
        db.accumulate(session_id, cur_snapshot_input, cur_snapshot_output,
                      per_call_total_input, pc_output,
                      pc_cache_read, pc_cache_write)
    except Exception:
        pass

    # Replay estimation
    try:
        replay_info = tx_mod.estimate_next_replay(
            transcript_path,
            latest_call_input=per_call_total_input,
            total_input_tokens=cur_snapshot_input,
            ctx_window_size=ctx_size,
        )
        replay_tokens = replay_info.get("estimated_tokens", 0)
        transcript_turns = replay_info.get("turn_count", 0)
    except Exception:
        replay_tokens = per_call_total_input or 0
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
    agg = db.get_all_totals()
    cum_input = agg["tot_input_tokens"]
    cum_output = agg["tot_output_tokens"]
    cum_cache_total = agg["tot_cache_read_tokens"] + agg["tot_cache_write_tokens"]
    tool_call_count = agg["tool_call_count"]
    subagent_total = agg["subagent_total"]
    subagent_running = agg["subagent_running"]

    # Cost: pass per-call cache ratio, not cumulative
    try:
        cost_str = cost_mod.fmt_cost(model_id, cum_input, cum_output,
                                     pc_cache_read, pc_input)
    except Exception:
        cc_cost = cost_data.get("total_cost_usd", 0) if isinstance(cost_data, dict) else 0
        cost_str = f"${cc_cost:.2f}" if cc_cost else "-"

    try:
        last_cost_str = cost_mod.fmt_last_cost(model_id, pc_input, pc_output, pc_cache_read)
    except Exception:
        last_cost_str = "-"

    try:
        pred_output = int(pc_output * 1.25)
        pred_cache = int(replay_tokens * (pc_cache_read / max(pc_cache_read + pc_input, 1)))
        pred_cost_str = cost_mod.fmt_last_cost(model_id, replay_tokens - pred_cache, pred_output, pred_cache)
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
            cost_str=cost_str,
            last_cost_str=last_cost_str,
            pred_cost_str=pred_cost_str,
            session_start_ts=session_start_ts,
            turn_count=turn_count,
            tool_call_count=tool_call_count,
            subagent_total=subagent_total,
            subagent_running=subagent_running,
            ctx_window_size=ctx_size,
        )
        print(output)
    except Exception as exc:
        print(f"\033[31mccs error: {exc}\033[0m", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
