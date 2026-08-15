"""Statusline display CLI. Called by Claude Code on every statusline tick."""

import json
import sys

from . import db
from . import cost as cost_mod
from . import transcript as tx_mod
from . import renderer
from . import usage as usage_mod
from . import balance as balance_mod
from .renderer import _fmt_model_name
from .balance import provider_for as _balance_provider
from .balance import resolve_actual_model_id as _resolve_actual_model_id
from .i18n import t
from .util import read_stdin_json


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
    # 解析真实模型名：代理模式下 claude-opus-4-8 → deepseek-v4-pro 等
    actual_model_id = _resolve_actual_model_id(model_id)
    display_name = model.get("display_name", "") if isinstance(model, dict) else ""
    model_name = _fmt_model_name(model_id, display_name)
    cost_data = data.get("cost", {})
    ctx = data.get("context_window", {})

    # 真实提供商检测：代理模式下 model_id 可能为 claude*，但实际后端是 DeepSeek 等。
    # 统一用 balance.provider_for 判断（含代理检测），不再单独写 _is_claude_model。
    balance_provider = _balance_provider(model_id)
    official_usage = None
    balance = None
    if balance_provider == "anthropic":
        rate_limits = data.get("rate_limits") or {}
        official_usage = _resolve_official_usage(data.get("version"), rate_limits)
    elif balance_provider:
        try:
            balance = balance_mod.get_balance(model_id)
        except Exception:
            balance = None

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
        raw_metrics = tx_mod.get_session_metrics(transcript_path)
        # 将 JSONL 里的代理模型名（如 claude-opus-4-8-2）映射为实际模型名
        # （如 deepseek-v4-pro），写入 DB 后定价查表精确匹配，不退化到 Anthropic 定价。
        raw_mu = raw_metrics.get("model_usage", {})
        mapped_mu: dict[str, dict] = {}
        for mid, usage in raw_mu.items():
            actual = _resolve_actual_model_id(mid)
            if actual not in mapped_mu:
                mapped_mu[actual] = dict(usage)
            else:
                for k in ("input", "output", "cache_read", "cache_write"):
                    mapped_mu[actual][k] = mapped_mu[actual].get(k, 0) + usage.get(k, 0)
        # 逐调用明细同样映射模型名——供潮汐定价按每次调用时间精确计价
        raw_calls = raw_metrics.get("model_calls", {})
        mapped_calls: dict[str, list] = {}
        for mid, calls in raw_calls.items():
            actual = _resolve_actual_model_id(mid)
            mapped_calls.setdefault(actual, []).extend(calls)
        metrics = {**raw_metrics, "model_usage": mapped_mu, "model_calls": mapped_calls}
        db.update_session_tokens(session_id, metrics)
        if mapped_mu:  # 仅当有数据时才写入——防止 transcript_path 为空时清空已有数据
            db.update_model_usage(session_id, mapped_mu)
        compaction_count = metrics.get("compaction_count", 0)
    except Exception:
        metrics = {}
        compaction_count = 0

    # CTX: transcript 稳态 context_len 为主；与 stdin snapshot 交叉校验，
    # 避免 transcript 尖峰/解析异常时独自报飞。
    tx_context_len = int(metrics.get("context_len", 0) or 0)
    snapshot_ctx_len = 0
    if snapshot_pct is not None and ctx_size > 0:
        try:
            snapshot_ctx_len = int(float(snapshot_pct) / 100.0 * ctx_size)
        except (TypeError, ValueError):
            snapshot_ctx_len = 0
    if snapshot_ctx_len <= 0 and per_call_total_input > 0:
        snapshot_ctx_len = per_call_total_input

    context_len = tx_context_len
    if tx_context_len > 0 and snapshot_ctx_len > 0:
        lo = min(tx_context_len, snapshot_ctx_len)
        hi = max(tx_context_len, snapshot_ctx_len)
        # 两者偏离过大时取更小值：尖峰高报比偶发低估更常见、更误导
        if lo > 0 and hi > int(lo * 1.35) and (hi - lo) > 20_000:
            context_len = lo
    elif tx_context_len <= 0:
        context_len = snapshot_ctx_len

    if context_len > 0 and ctx_size > 0:
        ctx_pct = context_len / ctx_size * 100
    else:
        ctx_pct = snapshot_pct

    # Replay estimation: 用交叉校验后的 context_len
    try:
        replay_info = tx_mod.estimate_next_replay(
            transcript_path,
            latest_call_input=per_call_total_input,
            total_input_tokens=cur_snapshot_input,
            ctx_window_size=ctx_size,
            current_context_len=context_len,
        )
        replay_tokens = replay_info.get("estimated_tokens", 0)
        transcript_turns = replay_info.get("turn_count", 0)
    except Exception:
        replay_tokens = per_call_total_input or context_len or 0
        transcript_turns = 0

    session = {}
    try:
        session = db.get_session(session_id) or {}
    except Exception:
        pass

    # turn_count 现为文本 user 轮次；优先 transcript，DB 仅作回退
    metrics_turns = int(metrics.get("turn_count", 0) or 0)
    db_turns = int(session.get("turn_count") or 0)
    turn_count = metrics_turns or transcript_turns or db_turns

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
        # TOTAL：优先用带时间戳的逐调用明细精算（潮汐定价按调用时刻选峰谷价）；
        # 无明细时回退 DB 按模型聚合（以当前时刻计价）。
        cost_str = cost_mod.fmt_cost_multi(
            db.get_model_breakdown(session_id),
            primary_model_id=actual_model_id or None,
            model_calls=metrics.get("model_calls"),
        )
    except Exception:
        cc_cost = cost_data.get("total_cost_usd", 0) if isinstance(cost_data, dict) else 0
        cost_str = f"${cc_cost:.2f}" if cc_cost else "-"

    try:
        last_cost_str = cost_mod.fmt_last_cost(actual_model_id, pc_input, pc_output, pc_cache_read, pc_cache_write)
    except Exception:
        last_cost_str = "-"

    try:
        pred_output = int(pc_output * 1.25)
        pc_total = max(per_call_total_input, 1)
        pred_cache = int(replay_tokens * pc_cache_read // pc_total)
        pred_cache_write = int(replay_tokens * pc_cache_write // pc_total)
        pred_input = max(0, replay_tokens - pred_cache - pred_cache_write)
        pred_cost_str = cost_mod.fmt_last_cost(actual_model_id, pred_input, pred_output, pred_cache, pred_cache_write)
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
            turn_count=turn_count,
            tool_call_count=tool_call_count,
            subagent_total=subagent_total,
            subagent_running=subagent_running,
            compaction_count=compaction_count,
            ctx_window_size=ctx_size,
            official_usage=official_usage,
            balance=balance,
        )
        print(output)
    except Exception as exc:
        print(f"\033[31mccs error: {exc}\033[0m", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
