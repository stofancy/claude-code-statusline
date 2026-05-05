"""ANSI rendering engine for the multi-line statusline.

256-colour health indicators: 64-level green→yellow→orange→red ramp.
▓░ bar: filled=health colour, empty=same hue dimmed.
"""

import time

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
    else:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h{m:02d}m"


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
    session_start_ts: int | None,
    turn_count: int,
    tool_call_count: int,
    subagent_total: int,
    subagent_running: int,
    ctx_window_size: int = 1_000_000,
) -> str:
    ctx_pct = ctx_pct or 0

    # Row 1: MODEL │ CTX bar XX% │ NEXT tok [cost] │ TOTAL cost │ LAST cost
    model_str = f"{_BOLD}{_CYAN}{model_name}{_RESET}"
    ctx_color = _health_color(ctx_pct)
    ctx_bar = _bar(ctx_pct)
    ctx_str = f"CTX {ctx_bar} {ctx_color}{ctx_pct:.0f}%{_RESET}"

    next_str: str
    if replay_tokens > 0:
        rp_color = _color_for_replay(replay_tokens, ctx_window_size)
        next_str = f"NEXT {rp_color}{_fmt_tokens(replay_tokens)}{_RESET} [\033[33m{pred_cost_str}{_RESET}]"
    else:
        next_str = f"NEXT {_DIM}-{_RESET}"

    cost_d = f"{_DIM}TOTAL{_RESET} \033[33m{cost_str}{_RESET}" if cost_str else ""
    last_d = f"{_DIM}LAST{_RESET} \033[33m{last_cost_str}{_RESET}" if last_cost_str else ""

    sep = f"{_DIM}│{_RESET}"
    row1 = f"{model_str} {sep} {ctx_str} {sep} {next_str} {sep} {cost_d} {sep} {last_d}"

    # Row 2: TURNS │ IN │ OUT │ CACHE XX.XXX% amount │ TOOLS │ AGENTS │ duration
    turn_str = f"{_DIM}TURNS{_RESET} {_BOLD}{turn_count}{_RESET}"
    in_str = f"{_DIM}IN{_RESET} {_BOLD}{_fmt_tokens(total_input)}{_RESET}"
    out_str = f"{_DIM}OUT{_RESET} {_BOLD}{_fmt_tokens(total_output)}{_RESET}"

    cache_rate = (cache_read * 100 / total_input) if total_input > 0 else 0
    cache_color = _health_color(100 - cache_rate)
    cache_str = f"{_DIM}CACHE{_RESET} {cache_color}{cache_rate:.3f}%{_RESET} {_fmt_tokens(cache_read)}"

    tool_str = f"{_DIM}TOOLS{_RESET} {_MAGENTA}{tool_call_count}{_RESET}"
    if subagent_running > 0:
        agent_str = f"{_DIM}AGENTS{_RESET} {_MAGENTA}{subagent_total}{_RESET}/{_GREEN}{subagent_running}r{_RESET}"
    else:
        agent_str = f"{_DIM}AGENTS{_RESET} {_MAGENTA}{subagent_total}{_RESET}"

    if session_start_ts:
        elapsed = int(time.time()) - session_start_ts
        dur_str = f"{_DIM}{_fmt_duration(elapsed)}{_RESET}"
    else:
        dur_str = f"{_DIM}-{_RESET}"

    row2 = f"{turn_str} {sep} {in_str} {sep} {out_str} {sep} {cache_str} {sep} {tool_str} {sep} {agent_str} {sep} {dur_str}"

    return f"{row1}\n{row2}"
