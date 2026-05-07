# Claude Code Statusline

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Production-grade custom statusline for [Claude Code](https://code.claude.com) optimised for **multi-provider** usage (DeepSeek, Anthropic, OpenAI, Gemini). Dual-line ANSI display with accurate cost calculation, JSONL-transcript-based token metrics, cache hit rate, compaction detection, and subagent aggregation.

## Why

Claude Code's built-in `cost.total_cost_usd` assumes Anthropic USD pricing. If you use DeepSeek (¥/CNY), Gemini, or OpenAI, the built-in cost is meaningless. More critically, `context_window.total_input_tokens` resets after each context compaction, making incremental snapshot-diff accumulation unreliable. This tool:

- Parses Claude Code's **on-disk JSONL transcript** directly for authoritative token counts
- Calculates cost using **actual provider pricing tables** (multi-currency, per-model)
- Shows **cache hit rate** as a proper 0–100% metric
- Detects and displays **compaction count** (`cN` suffix on turns)
- Estimates **next-turn replay tokens** to warn before context overflow
- Aggregates **subagent transcript tokens** (from `subagents/agent-*.jsonl`) into main session

## Display

```
DeepSeek-V4-Pro │ CTX ▓▓▓░░░░░ 38% │ NEXT 248.000k→150 [¥0.014] │ TOTAL ¥0.80 │ LAST ¥0.012
TURNS 47c2 │ IN 512.384k │ OUT 84.030k │ CACHE 96.351% 13.376M │ TOOLS 63 │ AGENTS 2/1r │ 57m35s
```

**Row 1:** Model name · context pressure (64-level gradient bar + %) · replay estimate with predicted cost · session cumulative cost · last-turn cost

**Row 2:** Turn count (`cN` = N compactions) · non-cache input tokens · output tokens · cache hit rate + absolute · tool calls · subagents (total/running) · session duration

Colour rules: green → yellow → red for context pressure (0–100%); green → yellow → red for cache health (inverted — high hit rate is green). 256-colour 64-level ANSI ramp.

## Quick Start

```bash
git clone https://github.com/stofancy/claude-code-statusline.git
cd claude-code-statusline
bash install.sh
```

Or via pip:

```bash
pip install git+https://github.com/stofancy/claude-code-statusline.git
```

Then merge the hook + statusline configuration into `~/.claude/settings.json` (see `examples/settings.json`). Restart Claude Code.

## Requirements

- Python 3.11+
- `pyyaml` (auto-installed)
- SQLite3 (stdlib)

## Pricing

Built-in pricing table at `src/ccs/pricing.yaml`. Override with `~/.claude/statusline/pricing.yaml`.

All prices in **CNY (¥)** per million tokens.

| Provider | Model | Input | Cache Hit | Cache Write | Output |
|----------|-------|-------|-----------|-------------|--------|
| DeepSeek | V4-Pro (2.5× off) | 3.00 | 0.025 | — | 6.00 |
| DeepSeek | V4-Flash | 1.00 | 0.02 | — | 2.00 |
| DeepSeek | R1 | 4.00 | 0.25 | — | 16.00 |
| Anthropic | Opus 4.7 | 107.73 | 10.77 | 215.46 | 538.65 |
| Anthropic | Sonnet 4.6 | 21.55 | 2.15 | 43.09 | 107.73 |
| Anthropic | Haiku 4.5 | 7.18 | 0.72 | 14.37 | 35.91 |
| OpenAI | GPT-4o | 17.95 | 8.98 | — | 71.80 |
| OpenAI | GPT-4o-mini | 1.08 | 0.54 | — | 4.31 |
| OpenAI | GPT-5 | 8.98 | 1.80 | — | 71.80 |
| Google | Gemini 2.5 Pro | 8.98 | 0.89 | — | 35.91 |
| Google | Gemini 2.5 Flash | 1.08 | 0.11 | — | 4.31 |

DeepSeek V4-Pro 2.5× discount valid until 2026/05/31 23:59 Beijing time. Update `pricing.yaml` when it changes.

If you use USD-based providers, convert their prices to CNY and override via user pricing file, or set `default_currency: USD` and update provider prices accordingly.

## Architecture

```
                        ┌─ Stop hook ───────────────────┐
                        ├─ PostToolUse ─── ccs-tracker ──┤
                        ├─ SubagentStart ── (hook events) ├── SQLite
                        └─ SubagentStop ─────────────────┘  (tool calls, subagent events)

Claude Code statusline tick (every 15s) → ccs-statusline
  ├── stdin JSON ─── model, cost, context_window, transcript_path
  ├── JSONL transcript ─── parse message.usage entries → dedup → per-model aggregation
  ├── subagents/agent-*.jsonl ─── aggregate subagent token usage
  ├── SQLite ─── write parsed values to sessions + model_usage tables
  └── stdout ─── 2-line ANSI text
```

### Modules

| File | CLI Entry | Purpose |
|------|-----------|---------|
| `statusline.py` | `ccs-statusline` | Orchestration: stdin → transcript → DB → render |
| `tracker.py` | `ccs-tracker` | Hook event collector → persist to SQLite |
| `db.py` | — | SQLite schema (4 tables), direct write, CRUD, cleanup |
| `cost.py` | — | Multi-provider pricing, per-model cost summation, model ID resolution |
| `transcript.py` | — | JSONL parsing, dedup, token metrics, subagent aggregation, replay estimation |
| `renderer.py` | — | 256-colour bars, token formatting, 2-line layout |
| `util.py` | — | Shared stdin JSON reader |
| `pricing.yaml` | — | Configurable provider pricing table |

### How Token Counting Works

Token statistics are read **directly from Claude Code's JSONL transcript** — the authoritative record of every API call. This avoids the three problems of snapshot-diff accumulation:

1. **Compaction oscillation**: `total_input_tokens` resets after compaction, causing phantom "new calls" on every tick during context refill. JSONL entries are immutable — compaction inserts a `compact_boundary` marker but never deletes historical entries.
2. **Multi-call merging**: Multiple API calls between 15-second ticks merge into one accumulation. JSONL records every call individually.
3. **cache_read double-counting**: The formula `per_call_total_input = input + cache_read` inflates input. JSONL separates `input_tokens` (non-cache) from `cache_read_input_tokens` (cache hit).

**Dedup strategy**: consecutive usage entries with identical `(input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)` are streaming duplicates — only the last in each group is kept. User messages break the consecutive chain to prevent cross-turn false dedup.

### How Subagents Are Aggregated

Claude Code stores subagent API calls in `subagents/agent-*.jsonl` files (under the same session directory as the main transcript). These files use the same JSONL format and are parsed identically.

Subagents often use a different model (e.g. `deepseek-v4-flash` for subagents vs `deepseek-v4-pro` for the main session). Each model's tokens are tracked independently and priced at its own rate — mixed-model sessions produce accurate cumulative costs.

## Configuration

### Hook Events

| Hook | Purpose |
|------|---------|
| `Stop` | Turn boundary marker |
| `PostToolUse` | Counts tool calls |
| `PostToolUseFailure` | Counts failed tool calls |
| `SubagentStart` | Tracks subagent spawn |
| `SubagentStop` | Tracks subagent completion |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `CCS_DEBUG=1` | Write raw hook data to `~/.claude/statusline/debug.log` |

## Database

Location: `~/.claude/statusline/usage.db` (SQLite, WAL mode).

| Table | Contents |
|-------|----------|
| `sessions` | Session metadata, cumulative token counters, turn count, tool/subagent counters |
| `model_usage` | Per-model `(session_id, model_id)` token breakdown for accurate cost calculation |
| `tool_calls` | Per-turn tool call records |
| `subagent_events` | Subagent start/stop events |

Sessions older than 30 days are auto-cleaned. Delete `usage.db` to reset all accumulated data.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Statusline not showing | Verify `statusLine` config in `~/.claude/settings.json`, restart Claude Code |
| Cost shows `-` | Check `pricing.yaml` exists; verify model ID matches a pricing entry |
| NEXT shows `-` | Normal — no transcript in early session; populates after first response |
| CACHE unusually high/low | Cache hit rate is `cache_read / (input + cache_read)`; >90% is normal for DeepSeek |
| TURNS shows `cN` | Compaction count — normal, indicates how many times context was compacted |
| Numbers look wrong after restart | Delete `usage.db` to reset cumulative counters |

## Compatibility

- Linux (x86_64, aarch64)
- macOS (Apple Silicon, Intel)
- Windows (WSL2)
- Windows Terminal with ANSI support

## License

MIT
