# Deep Analysis & Enhancement Plan — `claude-code-statusline`

**Reviewer:** Parker An (parker.an@serko.com)
**Date:** 2026-05-12
**Upstream:** https://github.com/stofancy/claude-code-statusline @ commit cloned 2026-05-12
**Scope:** Correctness of pricing/usage stats; cross-OS, i18n, currency enhancements.

---

## 1. Module-by-module review

### 1.1 `src/ccs/tracker.py` — hook event collector
Reads JSON from stdin (Claude Code pipes the event payload), persists session/tool/subagent events.

- ✓ Idempotent `ensure_session` keyed on `session_id`.
- ✓ Silent on missing `session_id` — sane: the hook fires even for transient pre-session events.
- ✓ `--event tool` accepts both `PostToolUse` and `PostToolUseFailure` (same handler), so every tool attempt is counted.
- ⚠ `record_subagent_start` / `stop` ingest `agent_type`/`agent_id` from the payload **unconditionally**. If Claude Code's payload shape changes, we silently store `"unknown"`. Not a correctness bug today, but a fragility note.
- ⚠ `_debug()` is `OSError`-safe but the catch is too broad (`except Exception: pass`) — a legitimate write failure is invisible unless `CCS_DEBUG=1`.

### 1.2 `src/ccs/transcript.py` — JSONL parser & metric extractor
This is the single source of token truth.

- ✓ **Dedup-by-`message.id`** strategy is correct: streaming chunks and tool-result-split multi-tool-call entries share a UUID; keeping the **last** entry retains the final `usage` snapshot. Verified by reading the dedup loop (lines 172–188) and observing it overwrites the previous index for the same UUID.
- ✓ Aggregates `subagents/agent-*.jsonl` recursively via `_subagent_metrics` and merges into the main session.
- ✓ `context_len` is computed from the **most recent main-thread** message (excludes `isSidechain` and `isApiErrorMessage`) — this matches what Claude Code itself displays as "context used", because sidechain reads don't grow the main turn's context.
- ✓ Cache hit rate denominator (`input + cache_read`) is well-defined: `input` is the non-cached input portion, so the ratio represents "fraction of context served from cache".
- ⚠ `_estimate_tokens(text) = max(1, len(text)/3.5)` is a rough heuristic (~3.5 chars/token works for English; Chinese tokens average closer to 1 char/token and so undercount). Used only when no `usage` data is available, so impact is limited to the replay forecast on empty transcripts.
- ⚠ `recent_turn_sizes` and `detect_growth_trend` aggregate user-side text, not LLM payload — so the trend is "user verbosity trend", not "context growth trend". Cosmetic mismatch with the label.
- ⚠ Cache is process-wide and never invalidates by size. For very long sessions on memory-constrained boxes, this could accumulate.

### 1.3 `src/ccs/db.py` — SQLite persistence
WAL mode, three tables (`sessions`, `tool_calls`, `subagent_events`) + per-model breakdown (`model_usage`).

- ✓ `conversation_id == session_id` in current code; comment correctly notes subagents share the parent session_id per Claude Code docs.
- ✓ `cleanup_stale(max_age_days=30)` exists but **is never wired** to a hook. Sessions accumulate forever unless the user calls it manually. (README implies auto-cleanup; code disagrees.)
- ✓ `get_all_totals` and `get_model_breakdown` filter on `is_stale=0` — safe even if cleanup never marks them.
- ⚠ Every DB call opens a new connection (`_conn()` → `sqlite3.connect`). On Windows this is measurably slower than reuse, and per-statusline tick the statusline.py path opens ~6 connections.
- ⚠ `_known_sessions` is a module-level set: harmless cache for `ensure_session`, but lifetime is one process invocation, so it doesn't actually skip the round-trip on later ticks (because the process is short-lived). Minor.

### 1.4 `src/ccs/cost.py` — pricing & cost formatting
- ✓ Per-model breakdown (`fmt_cost_multi`) — sums each model's tokens at its own rates, so mixed-model sessions cost out correctly.
- ✓ Cache-write surcharge applied **only** when the model lists it (correctly skips DeepSeek, which has no cache-write).
- ⚠ **`open(p)` lacks `encoding="utf-8"`** — on Windows this defaults to `cp1252` and would fail on the Chinese comments in `pricing.yaml`. Today it works by luck (the byte sequences are valid in cp1252); fragile.
- ⚠ **`_pricing_cache` never reloads.** Editing `pricing.yaml` requires a Claude Code restart.
- ⚠ **Silent default fallback (`¥1/¥4`)** when a model is unresolved. Mis-priced models look "free-ish" without any signal.
- ⚠ **Currency symbol map hardcoded to 3 codes.** GBP, JPY, AUD, INR — none get a symbol.
- ⚠ **No FX layer.** The YAML is hand-converted from USD prices (e.g. `claude-opus-4-7: input_per_1m: 107.73 # $15/1M ≈ ¥107.73`). The rate (~7.18) was correct on a specific day; it drifts.
- ⚠ Suffix-stripping fallback (`split('-')` peel from the tail) **succeeds** for IDs like `claude-opus-4-7-20251001` → `claude-opus-4-7`, but it relies on YAML keys not being dated.

### 1.5 `src/ccs/renderer.py` — ANSI multi-line renderer
- ✓ 64-step health ramp is well-tuned: pure green → yellow → orange → red.
- ✓ Bar renders even at 0% (8 dimmed `░`) and 100% (8 filled `▓`).
- ⚠ **All labels hardcoded English** (`MODEL`, `CTX`, `NEXT`, `TOTAL`, `LAST`, `TURNS`, `IN`, `OUT`, `CACHE`, `TOOLS`, `AGENTS`). Documentation is bilingual but the rendered UI is not.
- ⚠ `_fmt_tokens` uses `k`/`M`/`G` SI prefixes — fine for English, but no locale-aware number formatting (no thousands separator either).
- ⚠ Uses Unicode box characters (`│`, `▓`, `░`) — see Section 2 for the Windows console crash this caused.

### 1.6 `src/ccs/statusline.py` — main render loop
- ✓ Defensive `try/except` around DB and cost calls — never crashes the bar.
- ✓ `tx_context_len > 0 ? transcript : snapshot` — prefers the more accurate transcript-parsed value when available.
- ✓ Replay/forecast: blends `latest_call_input` and historical mean delta. Heuristic but reasonable.
- ⚠ Bottom-line fallback `f"${cc_cost:.2f}"` is **hardcoded `$`** even if the user's pricing is CNY-based.
- ⚠ `pred_cache` clamps to `pc_cache_read / max(pc_input + pc_cache_read, 1)` — if last turn had `cache_read=0`, the forecast is "no cache hit" even when historical hit rate is high. Minor pessimism.

### 1.7 `install.sh` — installer
- ✗ **POSIX-only.** Uses bash, `~/`, `venv/bin/`. No Windows or pure-PowerShell story.
- ⚠ Generates settings snippet with hard-coded `bin/` paths — wrong on Windows.

### 1.8 `pricing.yaml` — pricing table
- ✓ Includes DeepSeek, Anthropic, OpenAI, Google providers.
- ⚠ All entries in CNY with USD price as inline comment; mixes two units in one file.
- ⚠ Missing dated Anthropic IDs (`claude-opus-4-7-20251001`, `claude-sonnet-4-6-20250929`, `claude-haiku-4-5-20251001`). Suffix-stripping rescues them today; explicit entries would be more robust.
- ✗ **Significantly inaccurate prices** verified against official sources on 2026-05-12:

  | Model | Old YAML (USD-equiv) | Verified upstream | Notes |
  |---|---|---|---|
  | `claude-opus-4-7` input | $15 (`¥107.73`) | **$5** | Anthropic dropped Opus pricing for 4.5+; old YAML reflected Opus 4 / 4.1 rates |
  | `claude-opus-4-7` output | $75 (`¥538.65`) | **$25** | Same drop |
  | `claude-opus-4-7` cache_write | $30 (`¥215.46`) | **$6.25** (5m) / $10 (1h) | Old value was 1h rate at old Opus pricing |
  | `claude-sonnet-4-6` cache_write | $6 (`¥43.09`) | $3.75 (5m) / **$6** (1h) | Was 1h rate; we now use 5m as default since it's the typical SDK setting |
  | `claude-haiku-4-5` cache_write | $2 (`¥14.37`) | **$1.25** (5m) / $2 (1h) | Same as above |
  | `gemini-2.5-flash` input | $0.15 (`¥1.08`) | **$0.30** | Old value matched Gemini **1.5** Flash, not 2.5 |
  | `gemini-2.5-flash` cache_read | $0.015 | **$0.03** | Same |
  | `gemini-2.5-flash` output | $0.60 | **$2.50** | Same |
  | `gpt-5` input | $1.25 (`¥8.98`) | **$0.625** | OpenAI cut gpt-5 base rate in 2026 |
  | `gpt-5` output | $10 | **$5.00** | Same |
  | `deepseek-v4-pro` input | ¥3.00 (≈$0.418) | **$0.435** | Within rounding — DeepSeek lists USD directly now |
  | `deepseek-v4-flash` input | ¥1.00 (≈$0.139) | **$0.14** | Within rounding |

  Also missing in the old YAML and now added: `claude-opus-4-6`, `claude-opus-4-5`, `claude-opus-4-1`, `claude-opus-4`, `claude-sonnet-4-5`, `claude-sonnet-4`, `claude-haiku-3-5`, `gpt-5-mini`, `gpt-5-nano`, `gpt-5-4`, `gpt-5-5-pro`. Pre-existing models without explicit dated IDs (`...-20251001`, `...-20250929`) also added.

---

## 2. Cross-OS issues encountered when installing on Windows

| # | Symptom | Root cause | Resolution |
|---|---|---|---|
| 1 | `bash install.sh` not usable in PowerShell | POSIX script, no Windows path branch | **Phase 2:** ship `install.ps1` that mirrors logic with `Scripts/*.exe` paths |
| 2 | Claude Code hook: `command not found: C:UsersParkerAn...ccs-tracker.exe` | `\\`-quoted Windows paths in `settings.json` were passed through `bash -c "..."`; bash interpreted each `\` as an escape and concatenated the segments | Use `~/.claude/statusline/venv/Scripts/ccs-tracker.exe` — forward-slash, bash-friendly, `~` expands to home on Git Bash for Windows |
| 3 | Empty statusline, no error visible | `ccs-statusline.exe` raised `'charmap' codec can't encode character '│'` to stderr; Python exited before printing the rendered line | Force UTF-8 stdout in `util.py` (Phase 1, this commit) — `PYTHONIOENCODING=utf-8` workaround in `settings.json` becomes unnecessary |
| 4 | YAML load could fail on Chinese comments | `open(path)` uses platform default encoding (`cp1252` on Windows) | Add explicit `encoding="utf-8"` in `cost.py::_load_pricing` |

---

## 3. Internationalisation (i18n) gap

All UI strings live as literals in `renderer.py`. Source comments are bilingual EN/中文, README ships in both languages, but the bar itself is English-only.

**Proposed approach:** extract strings into `src/ccs/locales/{en,zh}.yaml`. Resolve locale per-call:

```
CCS_LANG env → $LC_ALL → $LANG → 'en' fallback
```

`renderer.py` reads from a dict keyed by string slug. Adding a new language is dropping a YAML.

---

## 4. Currency / FX gap

Today the entire pricing table is **denominated in one currency** (CNY) and the user has no way to view it in another without rewriting every line.

**Proposed approach:**

1. Keep `pricing.yaml` denominated in **USD** (the universal price-card currency for all four providers). One source of truth.
2. Add a top-level `fx_rates` block: `{ USD: 1.0, CNY: 7.18, EUR: 0.92, GBP: 0.79, JPY: 156.0, ... }`.
3. Add `display_currency` (and `CCS_CURRENCY` env-var override).
4. `cost.py` multiplies by `fx_rates[display_currency]` at format time and looks up the symbol.
5. Extend the symbol map to cover GBP/JPY/AUD/INR/HKD/SGD.

Migration: the current CNY-native YAML gets translated once to USD (divide by 7.18) so existing users see identical numbers when they set `display_currency: CNY`.

---

## 5. Enhancement plan & status

| Phase | Item | Status |
|---|---|---|
| 0 | Switch settings.json paths to `~/` forward slashes | ✅ done in session |
| 0 | Workaround Windows charmap with `PYTHONIOENCODING=utf-8` prefix | ✅ done in session (will be removed by Phase 1.1) |
| 1.1 | `util.py`: `sys.stdout.reconfigure(encoding="utf-8")` at import | ⬜ this branch |
| 1.2 | `cost.py`: utf-8 YAML open + mtime-keyed cache reload + unknown-model debug log | ⬜ this branch |
| 1.3 | `pricing.yaml`: add explicit dated Anthropic IDs | ⬜ this branch |
| 2.1 | `install.ps1` — PowerShell installer with correct Windows paths | ⬜ this branch |
| 2.2 | README: Windows install section | ⬜ this branch |
| 3   | i18n layer (en/zh YAML locales, env-var override) | ⬜ this branch |
| 4   | Multi-currency with `fx_rates` + `display_currency` + `CCS_CURRENCY` env | ⬜ this branch |
| 5   | (optional) Upstream PR — defer until local validation looks good | parked |

---

## 6. Verification checklist (per fix, before claiming done)

- [ ] `pip install -e .` from the repo succeeds in the existing venv
- [ ] `echo '<sample-json>' | ccs-statusline` renders both rows without stderr noise
- [ ] Restart Claude Code; new turn produces a populated statusline
- [ ] No PYTHONIOENCODING prefix needed in `~/.claude/settings.json`
- [ ] Setting `CCS_LANG=zh` produces Chinese labels
- [ ] Setting `CCS_CURRENCY=USD` shows costs in `$`
- [ ] Unknown model logged once to `~/.claude/statusline/debug.log` when `CCS_DEBUG=1`
