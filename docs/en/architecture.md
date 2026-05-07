# Architecture

> [中文文档](../zh/architecture.md)

## Overview

Claude Code Statusline is a two-line ANSI status display for Claude Code that provides accurate multi-provider token tracking and cost calculation. It replaces the built-in cost display with actual provider pricing tables and JSONL-transcript-based metrics.

## Data Flow

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

## CLI Entry Points

Two CLI commands are defined in `pyproject.toml`, both invoked by Claude Code with JSON piped via stdin:

| Command | Module | Caller | Purpose |
|---------|--------|--------|---------|
| `ccs-statusline` | `statusline.py:main` | Statusline refresh timer | Reads session JSON from stdin + accumulated data from SQLite → outputs 2-line ANSI to stdout |
| `ccs-tracker` | `tracker.py:main` | Hook events (Stop, PostToolUse, SubagentStart/Stop) | Persists event metadata to SQLite. Exit code always 0 to prevent hook errors |

## Module Breakdown

### `statusline.py` — Orchestration Pipeline

The main pipeline runs on every statusline tick (default every 15s):

1. **Read stdin**: Parse Claude Code's session JSON (model, cost, context_window, transcript_path)
2. **Parse transcript**: Call `get_session_metrics()` to extract token usage from JSONL
3. **Write to DB**: Persist parsed metrics to `sessions` and `model_usage` tables
4. **Read totals**: Aggregate all tokens from SQLite (including subagents)
5. **Calculate costs**: `fmt_cost_multi()` for cumulative cost, `fmt_last_cost()` for last turn
6. **Estimate replay**: `estimate_next_replay()` for NEXT prediction
7. **Render**: 2-line ANSI output

The stdin `current_usage` is preserved for LAST cost (most recent API call) and NEXT replay estimation (projecting recent turn averages).

### `tracker.py` — Hook Event Collector

Dispatches `--event` types to `db.py` handlers:

| Event | Action |
|-------|--------|
| `stop` | Ensure session exists, update metadata |
| `tool` | Record tool call, increment counter |
| `subagent-start` | Record start event, increment running count |
| `subagent-stop` | Record stop event, decrement running count |

### `db.py` — SQLite Persistence Layer

Database location: `~/.claude/statusline/usage.db` (WAL mode, auto-created).

Four tables:

- **`sessions`** — Session metadata, aggregate token counters, turn/subagent/tool counters
- **`model_usage`** — Per-model `(session_id, model_id)` token breakdown for accurate multi-model cost
- **`tool_calls`** — Per-turn tool call records
- **`subagent_events`** — Subagent start/stop events

Importantly, `update_session_tokens()` and `update_model_usage()` directly write JSONL-parsed values — no snapshot diff accumulation. Sessions use `conversation_id` for isolation across concurrent Claude Code instances. Stale sessions (>30 days) are auto-cleaned.

### `cost.py` — Multi-Provider Pricing

Pricing resolution chain:
```
~/.claude/statusline/pricing.yaml → built-in pricing.yaml → fallback defaults
```

Model ID matching:
1. Exact lookup
2. Strip `[1m]` suffix (DeepSeek context-window marker)
3. Stripping trailing version segments progressively (e.g., `gpt-4o-20250501` → `gpt-4o`)

Two cost functions:
- `fmt_cost_multi()` — Sums costs per-model from `model_usage` breakdown, supporting mixed-model sessions (e.g., main = deepseek-v4-pro, subagent = deepseek-v4-flash)
- `fmt_last_cost()` — Single-call cost using stdin `current_usage` values, with Anthropic `cache_write` support

### `transcript.py` — JSONL Transcript Parser

**Core function**: `get_session_metrics(transcript_path)`

Approach: Parse all JSONL entries containing `message.usage`, deduplicate streaming duplicates, group by `message.model`.

**Dedup strategy**: Consecutive entries with identical `(input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens)` are streaming duplicates — only the last in each group is retained. User messages break the chain to prevent cross-turn false dedup.

**Subagent aggregation**: `_subagent_metrics()` scans `{session_id}/subagents/agent-*.jsonl`, parses each identically, and merges into main metrics. Subagents may use different models (e.g., flash vs pro) — each model is tracked and priced independently.

**Replay estimation**: `estimate_next_replay()` averages recent turn sizes from `recent_turn_sizes()` and projects them against the latest API call input. Growth trends are detected via `detect_growth_trend()` using a half-split ratio comparison (>15% change = rising/declining).

### `renderer.py` — ANSI Rendering

256-colour engine with 64-level health gradient (green → yellow → orange → red) for two indicators:

- **CTX bar**: Context pressure at current level
- **CACHE percentage**: Cache hit rate (inverted — high hit rate = green)

Output format:
```
DeepSeek-V4-Pro │ CTX ▓▓▓░░░░░ 38% │ NEXT 248.000k→150 [¥0.014] │ TOTAL ¥0.80 │ LAST ¥0.012
TURNS 47c2 │ IN 512.384k │ OUT 84.030k │ CACHE 96.351% 13.376M │ TOOLS 63 │ AGENTS 2/1r │ 57m35s
```

**Row 1**: Model name · CTX bar + percentage · replay estimate with predicted cost · cumulative session cost · last-turn cost

**Row 2**: Turns (`cN` = N compactions) · non-cache input tokens · output tokens · cache hit rate + absolute · tool calls · subagents (total/running) · session duration

## Key Design Decisions

### JSONL Transcript Over Snapshot Diff

Token statistics are read **directly from Claude Code's JSONL transcript** rather than accumulating `context_window.total_input_tokens` snapshots. This avoids:

1. **Compaction oscillation**: `total_input_tokens` resets after compaction, causing phantom "new calls" on every tick. JSONL entries are immutable — compaction inserts a `compact_boundary` marker but never deletes history.
2. **Multi-call merging**: Multiple API calls between 15-second ticks merge into one accumulation. JSONL records every call individually.
3. **cache_read double-counting**: The formula `per_call_total_input = input + cache_read` inflates totals. JSONL separates `input_tokens` (non-cache) from `cache_read_input_tokens`.

### Conversation Isolation

Each Claude Code conversation is identified by `session_id`. Subagents share the same `session_id` (differentiated by `agent_id`), so all hook events naturally aggregate to the same session — no subagent linking logic needed. The `conversation_id` column is reserved for future use; currently always equals `session_id`.
