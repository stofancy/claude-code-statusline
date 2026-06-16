"""ANSI rendering engine for the multi-line statusline.

256-colour health indicators: 64-level green→yellow→orange→red ramp.
▓░ bar: filled=health colour, empty=same hue dimmed.
"""

import time

from .i18n import t
from .cost import _symbol_for

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


def _strip_zeros(s: str) -> str:
    """去除格式化数字字符串中多余的尾零：1.000k → 1k, 8.00 → 8。"""
    if "." not in s:
        return s
    # 后缀字母 (k/M/G) 或无后缀纯数字
    if s[-1].isalpha():
        head, suffix = s[:-1], s[-1]
    else:
        head, suffix = s, ""
    head = head.rstrip("0").rstrip(".")
    return head + suffix


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        s = f"{n/1_000_000_000:.3f}G"
    elif n >= 1_000_000:
        s = f"{n/1_000_000:.3f}M"
    elif n >= 1_000:
        s = f"{n/1_000:.3f}k"
    else:
        return str(n)
    return _strip_zeros(s)


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


def _fmt_amount(n: float) -> str:
    """Compact numeric amount for budgets/credits: 8176 → `8.18k`, 25000 → `25k`."""
    if n >= 1_000_000:
        s = f"{n/1_000_000:.2f}M"
    elif n >= 1_000:
        s = f"{n/1_000:.2f}k"
    else:
        s = f"{n:.0f}"
    return _strip_zeros(s)


def _fmt_window(label: str, pct: float | None, resets_at: int | None) -> str:
    """Single rolling window: `5H 24% 4h57m`."""
    label = t(label)
    if pct is None:
        return f"{_DIM}{label} -{_RESET}"
    c = _health_color(pct)
    s = f"{_DIM}{label}{_RESET} {c}{pct:>3.0f}%{_RESET}"
    if resets_at is not None:
        rem = resets_at - int(time.time())
        if rem > 0:
            s += f" {_DIM}{_fmt_duration(rem)}{_RESET}"
    return s


def _fmt_dollars(cents: float) -> str:
    """美元金额：端点以美分计，转为美元。预算通常较小，不做 k 压缩。
    整数显示无小数（`$250`），含零头保留两位（`$84.03`）。"""
    d = cents / 100
    return f"${d:.0f}" if abs(d - round(d)) < 0.005 else f"${d:.2f}"


def _fmt_monthly(monthly: dict) -> str:
    """Monthly extra-usage budget. USD: `MO $84.03/$250 34%`; credits: `MO 8.18k/25k 33%`."""
    used = monthly.get("used") or 0
    limit = monthly.get("limit") or 0
    pct = monthly.get("utilization")
    if pct is None and limit > 0:
        pct = used / limit * 100
    pct = pct or 0
    c = _health_color(pct)
    if monthly.get("currency") == "USD":
        # 端点以美分计量美元预算（25000 == $250）。
        amt = f"{_fmt_dollars(used)}/{_fmt_dollars(limit)}"
    else:
        amt = f"{_fmt_amount(used)}/{_fmt_amount(limit)}"
    return f"{_DIM}{t('MO')}{_RESET} {amt} {c}{pct:>3.0f}%{_RESET}"


def _fmt_balance(balance: dict) -> str:
    """Format provider balance/credits/quota for the status line.

    DeepSeek (monetary): ``BAL ¥110.00``
    OpenAI (credits):    ``BAL $4.90``
    Zhipu (quota):       ``5H 73%│7D 73%│MO 99%``  (标签自描述，无 BAL 前缀)
    MiniMax (quota):     ``general 97%│abab6.5 50%``
    """
    ptype = balance.get("type", "")
    label = f"{_DIM}{t('BAL')}{_RESET}"
    if ptype == "monetary":
        cur = balance.get("currency", "USD")
        amt = balance.get("balance", 0)
        symbol = _symbol_for(cur)
        return f"{label} \033[33m{symbol}{amt:g}\033[0m"
    if ptype == "credits":
        amt = balance.get("balance", 0)
        return f"{label} \033[33m${amt:.2f}\033[0m"
    if ptype == "quota":
        quotas = balance.get("quotas", [])
        sep_q = f"{_DIM}│{_RESET}"
        parts = []
        for q in quotas:
            pct = q.get("remaining_pct", 0)
            name = q.get("short") or q.get("name", "?")
            parts.append(f"{_DIM}{name}{_RESET} {_health_color(100-pct)}{pct}%{_RESET}")
        return sep_q.join(parts)
    return ""


def _fmt_official_usage(usage: dict) -> str:
    """Combined official-usage segment: rolling windows + monthly budget."""
    parts = []
    for w in usage.get("windows") or []:
        parts.append(_fmt_window(w["label"], w.get("utilization"), w.get("resets_at")))
    monthly = usage.get("monthly")
    if monthly:
        parts.append(_fmt_monthly(monthly))
    sep = f"{_DIM}│{_RESET}"
    return sep.join(parts)


# ── Model display name ──────────────────────────────────────────────────────

_BRAND_OVERRIDES: dict[str, str] = {
    "deepseek": "DeepSeek",
    "gpt": "GPT",
    "minimax": "MiniMax",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "claude": "Claude",
    "mistral": "Mistral",
    "llama": "LLaMA",
    "mimo": "MIMO",
    "zhipu": "Zhipu",
    "glm": "GLM",
}


def _fmt_model_name(model_id: str, display_name: str = "") -> str:
    """将模型 ID 转换为用户可读的显示名。

    优先级：
      1. stdin 提供的 ``display_name``（非空且不等于 model_id）
      2. 品牌感知格式化（resolve_actual_model_id → 品牌覆写 → 数字段保连字符）
      3. 原始 model_id 回退

    None / 空字符串安全：返回 ``""``。
    含 ``[1M]`` 后缀时追加 `` 1M`` 标记。
    """
    if not model_id:
        return ""

    # 优先级 1: stdin 提供的 display_name
    if display_name and display_name != model_id:
        return display_name

    # 优先级 2: 解析实际模型 ID + 品牌感知格式化
    from .balance import resolve_actual_model_id, _detect_proxy_provider

    resolved = resolve_actual_model_id(model_id)
    if not resolved:
        return model_id

    # 品牌感知格式化：连字符分段，每段独立处理大小写
    parts: list[str] = []
    for part in resolved.split("-"):
        if not part:
            continue
        # 数字段保持原样（"4", "20251001"）
        if part.isdigit():
            parts.append(part)
        # 数字+字母组合保持原样（"4o", "3.5"）
        elif part and part[0].isdigit():
            parts.append(part)
        else:
            lower = part.lower()
            if lower in _BRAND_OVERRIDES:
                parts.append(_BRAND_OVERRIDES[lower])
            elif part.isupper() and len(part) <= 5:
                # 短全部大写缩写保持全大写（如 "API"）
                parts.append(part)
            else:
                parts.append(part.capitalize())

    display = " ".join(parts)

    # [1M] 标记仅在非代理模式下追加——代理映射后的实际模型
    # （如 deepseek-v4-flash）并无 1M 上下文窗口，避免误导。
    if not _detect_proxy_provider():
        if "[1M]" in model_id or "[1m]" in model_id:
            display += " 1M"

    return display


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
    turn_count: int,
    tool_call_count: int,
    subagent_total: int,
    subagent_running: int,
    compaction_count: int = 0,
    ctx_window_size: int = 1_000_000,
    official_usage: dict | None = None,
    cache_write: int = 0,
    balance: dict | None = None,
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
        next_str = f"{_DIM}{t('NEXT')}{_RESET} {rp_color}{_fmt_tokens(replay_tokens)}{_RESET}{out_part} [\033[33m{pred_cost_str}{_RESET}]"
    else:
        next_str = f"{_DIM}{t('NEXT')}{_RESET} {_DIM}-{_RESET}"

    cost_d = f"{_DIM}{t('TOTAL')}{_RESET} \033[33m{cost_str}{_RESET}" if cost_str else ""
    last_d = f"{_DIM}{t('LAST')}{_RESET} \033[33m{last_cost_str}{_RESET}" if last_cost_str else ""

    sep = f"{_DIM}│{_RESET}"

    # Row 1（钱）：MODEL │ LAST │ TOTAL │ NEXT │ [budget]
    # 时间叙事：刚发生 → 累计 → 预测 → 配额天花板
    row1_parts = [model_str]
    if last_d:
        row1_parts.append(last_d)
    if cost_d:
        row1_parts.append(cost_d)
    row1_parts.append(next_str)

    budget_seg = ""
    if official_usage:
        budget_seg = _fmt_official_usage(official_usage)
    elif balance:
        budget_seg = _fmt_balance(balance)
    if budget_seg:
        row1_parts.append(budget_seg)
    row1 = sep.join(row1_parts)

    # Row 2（token）：CTX │ IN │ OUT │ CACHE │ TURNS │ TOOLS │ AGENTS │ duration
    # 上下文压力打头 → token 分解 → 活动计数 → 时长收尾
    if compaction_count > 0:
        turn_str = f"{_DIM}{t('TURNS')}{_RESET} {_BOLD}{turn_count}{_RESET}{_DIM}c{compaction_count}{_RESET}"
    else:
        turn_str = f"{_DIM}{t('TURNS')}{_RESET} {_BOLD}{turn_count}{_RESET}"
    in_str = f"{_DIM}{t('IN')}{_RESET} {_BOLD}{_fmt_tokens(total_input)}{_RESET}"
    out_str = f"{_DIM}{t('OUT')}{_RESET} {_BOLD}{_fmt_tokens(total_output)}{_RESET}"

    total_with_cache = total_input + cache_read + cache_write
    cache_rate = (cache_read * 100 / total_with_cache) if total_with_cache > 0 else 0
    cache_color = _health_color(100 - cache_rate)
    cache_str = f"{_DIM}{t('CACHE')}{_RESET} {cache_color}{cache_rate:.3f}%{_RESET} {_fmt_tokens(cache_read)}"

    tool_str = f"{_DIM}{t('TOOLS')}{_RESET} {_MAGENTA}{tool_call_count}{_RESET}"
    if subagent_running > 0:
        agent_str = f"{_DIM}{t('AGENTS')}{_RESET} {_MAGENTA}{subagent_total}{_RESET}/{_GREEN}{subagent_running}r{_RESET}"
    else:
        agent_str = f"{_DIM}{t('AGENTS')}{_RESET} {_MAGENTA}{subagent_total}{_RESET}"

    row2_parts = [ctx_str, in_str, out_str, cache_str, turn_str, tool_str, agent_str]
    row2 = sep.join(row2_parts)

    return f"{row1}\n{row2}"
