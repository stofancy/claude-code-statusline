"""Codex faux statusline CLI for a Stop hook.

Codex has a configurable built-in footer, but not a Claude Code-style custom
statusLine command. For Codex, this command is meant to run as a ``Stop`` hook
and return ``{"systemMessage": "..."}``, which Codex appends after the turn.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import codex_transcript
from . import cost as cost_mod
from . import renderer
from .renderer import _fmt_model_name


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Render a Claude-style faux statusline from Codex rollout JSONL.")
    p.add_argument("transcript", nargs="?", help="Path to a Codex rollout-*.jsonl file. Defaults to latest session.")
    p.add_argument("--sessions-dir", help="Override Codex sessions directory, default: $CODEX_HOME/sessions or ~/.codex/sessions.")
    p.add_argument("--ctx-window", type=int, default=0, help="Context window size. Defaults to transcript value, CODEX_MODEL_CONTEXT_WINDOW, or 128000.")
    p.add_argument("--no-color", action="store_true", help="Strip ANSI color from output.")
    p.add_argument("--plain", action="store_true", help="Print text directly instead of Codex hook JSON.")
    return p


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    hook_payload = _read_hook_payload()
    path = args.transcript or hook_payload.get("transcript_path")
    if not path:
        latest = codex_transcript.latest_rollout(args.sessions_dir)
        if latest is None:
            print("No Codex rollout transcript found", file=sys.stderr)
            sys.exit(1)
        path = str(latest)

    metrics = codex_transcript.session_metrics(path)
    model_id = metrics.get("model_id") or "unknown"
    effort = metrics.get("effort") or ""
    model_name = _fmt_model_name(model_id) + (f"/{effort}" if effort else "")
    ctx_window = args.ctx_window or int(metrics.get("context_window_size") or 0) or _env_int("CODEX_MODEL_CONTEXT_WINDOW", 128_000)
    ctx_pct = (metrics.get("context_len", 0) / ctx_window * 100) if ctx_window else 0
    last = metrics.get("last_usage") or {}

    try:
        cost_str = cost_mod.fmt_cost_multi(metrics.get("model_usage") or {}, primary_model_id=model_id)
    except Exception:
        cost_str = "-"

    try:
        last_cost_str = cost_mod.fmt_last_cost(
            model_id,
            int(last.get("input") or 0),
            int(last.get("output") or 0),
            int(last.get("cache_read") or 0),
            int(last.get("cache_write") or 0),
        )
    except Exception:
        last_cost_str = "-"

    pred_output = int((last.get("output") or 0) * 1.25)
    try:
        pred_cost_str = cost_mod.fmt_last_cost(model_id, int(metrics.get("replay_tokens") or 0), pred_output, 0, 0)
    except Exception:
        pred_cost_str = "-"

    output = renderer.render(
        model_name=model_name,
        ctx_pct=ctx_pct,
        replay_tokens=int(metrics.get("replay_tokens") or 0),
        total_input=int(metrics.get("input") or 0),
        total_output=int(metrics.get("output") or 0),
        cache_read=int(metrics.get("cache_read") or 0),
        cache_write=int(metrics.get("cache_write") or 0),
        cost_str=cost_str,
        last_cost_str=last_cost_str,
        pred_cost_str=pred_cost_str,
        pred_output=pred_output,
        turn_count=int(metrics.get("turn_count") or 0),
        tool_call_count=int(metrics.get("tool_call_count") or 0),
        subagent_total=0,
        subagent_running=0,
        compaction_count=int(metrics.get("compaction_count") or 0),
        ctx_window_size=ctx_window,
        official_usage=metrics.get("official_usage"),
    )
    if args.no_color:
        output = _strip_ansi(output)

    if args.plain:
        print(output)
    else:
        print(json.dumps({"systemMessage": "\n" + output}, ensure_ascii=False))


def _read_hook_payload() -> dict:
    if sys.stdin.isatty():
        return {}
    try:
        data = json.load(sys.stdin)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _env_int(name: str, default: int) -> int:
    import os

    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


if __name__ == "__main__":
    main()
