"""ANSI rendering engine for the multi-line statusline.

256-colour health indicators: 64-level green→yellow→orange→red ramp.
▓░ bar: filled=health colour, empty=same hue dimmed.
"""

import time

from .i18n import t

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_MAGENTA = "\033[35m"

# 64-level health ramp — medium brightness, pure green→yellow→orange→red
_HEALTH64 = [
    35, 35, 36, 36, 41, 41, 42, 42,
    77, 77, 78, 78, 113, 113, 114, 114,
    142, 142, 143, 143, 178, 178, 179, 179,
    184, 184, 185, 185, 186, 186, 187, 187,
    172, 172, 173, 173, 174, 174, 208, 208,
    209, 209, 210, 210, 216, 216, 217, 217,
    167, 167, 168, 168, 169, 169, 170, 170,
    160, 160, 124, 124, 125, 125, 126, 126,
]


def _health_color(pct: float) -> str:
    idx = min(63, max(0, int(pct / 100 * 63)))
    return f"\033[38;5;{_HEALTH64[idx]}m"


def _bar(pct: float, width: int = 8) -> str:
    """▓░ bar. Filled=health colour, empty=same hue dimmed."""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    empty = width - filled
    c = _health_color(pct)
    sgr = c[2:-1]
    parts = [c, "▓" * filled]
    if empty:
        parts.extend([f"\033[2;{sgr}m", "░" * empty, _RESET])
    else:
        parts.append(_RESET)
    return "".join(parts)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.3f}G"
    elif n >= 1_000_000:
        return f"{n/1_000_000:.3f}M"
    elif n >= 1_000:
        return f"{n/1_000:.3f}k"
    else:
        return str(n)


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s}s"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h{m:02d}m"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}d{h:02d}h"


def _color_for_replay(tokens: int, ctx_window: int) -> str:
    if ctx_window <= 0:
        return _GREEN
    ratio = tokens / ctx_window
    if ratio < 0.5:
        return _GREEN
    elif ratio < 0.8:
        return "\033[33m"
    else:
        return _RED


def _fmt_rate_limits_combined(
    top_label: str,
    top_pct: float | None,
    top_resets_at: int | None,
    bot_label: str,
    bot_pct: float | None,
    bot_resets_at: int | None,
) -> str:
    """Compact rate-limit segment: `5H 24% 4h57m │ 7D 41% 6d03h`."""
    now = int(time.time())

    def _val(label: str, pct: float | None, resets_at: int | None) -> str:
        if pct is None:
            return f"{_DIM}{label} -{_RESET}"
        c = _health_color(pct)
        s = f"{_DIM}{label}{_RESET} {c}{pct:>3.0f}%{_RESET}"
        if resets_at is not None:
            rem = resets_at - now
            if rem > 0:
                s += f" {_DIM}{_fmt_duration(rem)}{_RESET}"
        return s

    sep = f"{_DIM}│{_RESET}"
    return f"{_val(top_label, top_pct, top_resets_at)} {sep} {_val(bot_label, bot_pct, bot_resets_at)}"


def render(
    model_name: str,
    ctx_pct: float | None,
    replay_tokens: int,
    total_input: int,
    total_output: int,
    cache_read: int,
    cost_str: str,
    last_cost_str: str,
    pred_cost_str: str,
    pred_output: int,
    session_start_ts: int | None,
    turn_count: int,
    tool_call_count: int,
    subagent_total: int,
    subagent_running: int,
    compaction_count: int = 0,
    ctx_window_size: int = 1_000_000,
    rate_limit_5h: float | None = None,
    rate_limit_5h_resets_at: int | None = None,
    rate_limit_7d: float | None = None,
    rate_limit_7d_resets_at: int | None = None,
) -> str:
    ctx_pct = ctx_pct or 0

    # Row 1: MODEL │ CTX bar XX% │ NEXT tok [cost] │ TOTAL cost │ LAST cost
    model_str = f"{_BOLD}{_CYAN}{model_name}{_RESET}"
    ctx_color = _health_color(ctx_pct)
    ctx_bar = _bar(ctx_pct)
    ctx_str = f"{t('CTX')} {ctx_bar} {ctx_color}{ctx_pct:.0f}%{_RESET}"

    next_str: str
    if replay_tokens > 0:
        rp_color = _color_for_replay(replay_tokens, ctx_window_size)
        out_part = f"{_DIM}→{_RESET}{_fmt_tokens(pred_output)}" if pred_output > 0 else ""
        next_str = f"{t('NEXT')} {rp_color}{_fmt_tokens(replay_tokens)}{_RESET}{out_part} [\033[33m{pred_cost_str}{_RESET}]"
    else:
        next_str = f"{t('NEXT')} {_DIM}-{_RESET}"

    cost_d = f"{_DIM}{t('TOTAL')}{_RESET} \033[33m{cost_str}{_RESET}" if cost_str else ""
    last_d = f"{_DIM}{t('LAST')}{_RESET} \033[33m{last_cost_str}{_RESET}" if last_cost_str else ""

    sep = f"{_DIM}│{_RESET}"
    row1 = f"{model_str} {sep} {ctx_str} {sep} {next_str} {sep} {cost_d} {sep} {last_d}"

    # Row 2: TURNS │ IN │ OUT │ CACHE XX.XXX% amount │ TOOLS │ AGENTS │ duration
    if compaction_count > 0:
        turn_str = f"{_DIM}{t('TURNS')}{_RESET} {_BOLD}{turn_count}{_RESET}{_DIM}c{compaction_count}{_RESET}"
    else:
        turn_str = f"{_DIM}{t('TURNS')}{_RESET} {_BOLD}{turn_count}{_RESET}"
    in_str = f"{_DIM}{t('IN')}{_RESET} {_BOLD}{_fmt_tokens(total_input)}{_RESET}"
    out_str = f"{_DIM}{t('OUT')}{_RESET} {_BOLD}{_fmt_tokens(total_output)}{_RESET}"

    total_with_cache = total_input + cache_read
    cache_rate = (cache_read * 100 / total_with_cache) if total_with_cache > 0 else 0
    cache_color = _health_color(100 - cache_rate)
    cache_str = f"{_DIM}{t('CACHE')}{_RESET} {cache_color}{cache_rate:.3f}%{_RESET} {_fmt_tokens(cache_read)}"

    tool_str = f"{_DIM}{t('TOOLS')}{_RESET} {_MAGENTA}{tool_call_count}{_RESET}"
    if subagent_running > 0:
        agent_str = f"{_DIM}{t('AGENTS')}{_RESET} {_MAGENTA}{subagent_total}{_RESET}/{_GREEN}{subagent_running}r{_RESET}"
    else:
        agent_str = f"{_DIM}{t('AGENTS')}{_RESET} {_MAGENTA}{subagent_total}{_RESET}"

    if session_start_ts:
        elapsed = int(time.time()) - session_start_ts
        dur_str = f"{_DIM}{_fmt_duration(elapsed)}{_RESET}"
    else:
        dur_str = f"{_DIM}-{_RESET}"

    row2_parts = [turn_str, in_str, out_str, cache_str, tool_str, agent_str, dur_str]

    if rate_limit_5h is not None or rate_limit_7d is not None:
        row2_parts.append(
            _fmt_rate_limits_combined(
                t("5H"), rate_limit_5h, rate_limit_5h_resets_at,
                t("7D"), rate_limit_7d, rate_limit_7d_resets_at,
            )
        )

    row2 = f" {sep} ".join(row2_parts)

    return f"{row1}\n{row2}"
