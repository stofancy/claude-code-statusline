# Claude Code Statusline

Production-grade custom statusline for [Claude Code](https://code.claude.com) optimized for **non-Anthropic providers** (DeepSeek, OpenAI, Gemini). Multi-line ANSI display with accurate cost calculation, real-time token metrics, cache efficiency tracking, and subagent aggregation.

## Why

Claude Code's built-in `cost.total_cost_usd` assumes Anthropic USD pricing. If you use DeepSeek (¥/CNY), Gemini, or OpenAI, the built-in cost is meaningless. This tool:

- Calculates cost using actual provider pricing tables
- Tracks cumulative token usage across API calls (Claude Code's `total_input_tokens` resets during context compaction)
- Shows cache hit ratio matching your provider dashboard (97%+ for DeepSeek)
- Estimates next-turn replay tokens to warn before context overflow
- Aggregates subagent token usage into the main session total

## Display

```
DeepSeek-V4-Pro │ CTX ▓▓▓░░░░░ 38% │ NEXT 248.000k [¥0.014] │ TOTAL ¥0.801 │ LAST ¥0.012
TURNS 12 │ IN 2.384M │ OUT 4.030k │ CACHE ▓▓▓▓▓▓▓▓ 99.651% 2.376M │ TOOLS 63 │ AGENTS 2/1r │ 57m35s
```

**Row 1:** Model name · context pressure (gradient bar + %) · next-turn replay estimate with predicted cost · session total cost · last-turn cost

**Row 2:** Turn count · cumulative input · cumulative output · cache hit ratio (3 decimal) + bar + absolute · tool calls · subagents (total/running) · session duration

Colour rules: green/yellow/red for context pressure; green/yellow/red for cache health (inverted). 256-colour 64-level health ramp, ANSI dim for bar empty portion.

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

Then merge the hook + statusline configuration into `~/.claude/settings.json` (see `examples/settings.json`).

Restart Claude Code. The statusline appears at the bottom of your terminal.

## Requirements

- Python 3.11+
- `pyyaml` (auto-installed)
- SQLite3 (stdlib)

## Pricing

Built-in pricing table at `src/ccs/pricing.yaml`. Override with `~/.claude/statusline/pricing.yaml`.

| Provider | Model | Input | Cache Hit | Output |
|----------|-------|-------|-----------|--------|
| DeepSeek | V4-Pro (2.5× off) | ¥3.00/M | ¥0.025/M | ¥6.00/M |
| DeepSeek | V4-Flash | ¥1.00/M | ¥0.02/M | ¥2.00/M |
| DeepSeek | R1 | ¥4.00/M | ¥0.25/M | ¥16.00/M |
| Anthropic | Opus 4.7 | $15/M | $1.50/M | $75/M |
| Anthropic | Sonnet 4.6 | $3/M | $0.30/M | $15/M |
| OpenAI | GPT-4o | $2.50/M | $1.25/M | $10/M |
| Google | Gemini 2.5 Pro | $1.25/M | $0.125/M | $5/M |

DeepSeek V4-Pro discount valid until 2026/05/31 23:59 Beijing time. Update `pricing.yaml` after.

Cost accounts for cache tokens correctly: subtracts cache-read from `total_input` before billing at full input rate, adds back at discount cache-hit rate. Uses latest call's ratio as estimate for cumulative totals.

## Architecture

```
Claude Code Stop hook       → ccs-tracker → SQLite (turn completion)
Claude Code PostToolUse     → ccs-tracker → SQLite (tool call counter)
Claude Code SubagentStart   → ccs-tracker → SQLite (subagent tracking)
Claude Code SubagentStop    → ccs-tracker → SQLite (subagent tracking)

Claude Code statusline tick → ccs-statusline
  ├── stdin ← session JSON (model, tokens, cost, transcript path)
  ├── SQLite ← aggregate current conversation (main + subagents)
  └── stdout → 2-line ANSI text
```

### Modules

| File | CLI Entry | Purpose |
|------|-----------|---------|
| `statusline.py` | `ccs-statusline` | Read stdin JSON + SQLite → render ANSI |
| `tracker.py` | `ccs-tracker` | Hook event collector → persist to SQLite |
| `db.py` | — | SQLite schema (4 tables), accumulate (双层), CRUD, cleanup |
| `cost.py` | — | Multi-provider pricing, `fmt_cost_multi` per-model cost summation |
| `transcript.py` | — | JSONL parsing, turn counting, replay estimation |
| `renderer.py` | — | 256-color bars, token formatting, 2-line layout |
| `util.py` | — | Shared stdin JSON reader |
| `pricing.yaml` | — | Configurable provider pricing table |

### How True Cumulative Values Are Tracked

双层追踪：`sessions` 表存储聚合总数用于显示，`model_usage` 表按模型独立追踪。每层使用独立的 snapshot 检测——当 `total_input_tokens` 变化时累加 per-call token 到累积计数器。模型层级的独立 snapshot 避免了主会话与子代理不同上下文之间 snapshot 值振荡导致的误检测。

### How Subagents Are Aggregated

Claude Code 官方文档确认：子代理与主会话**共享同一个 `session_id`**，仅通过额外的 `agent_id` 字段区分。因此所有 hook 事件（PostToolUse、SubagentStart/Stop、statusline tick）天然归入同一会话行，无需外部链接机制。

子代理可能使用不同模型（如 `deepseek-v4-flash` vs `deepseek-v4-pro[1m]`）。`model_usage` 表按模型独立追踪 token + snapshot，`fmt_cost_multi()` 按模型分别定价后加总，确保混合模型场景下的精确成本。

### How Cache Ratio Works

The statusline JSON's `current_usage.cache_read_input_tokens` is per-call (not cumulative). Since caching represents reused context (same content read in each call), we display the latest call's ratio as a representative snapshot:

```
cache_ratio = cache_read / (input_tokens + cache_read)
```

This typically matches DeepSeek's dashboard-reported ~97%+ hit rate.

## Configuration

### Hook Events

| Hook | Purpose |
|------|---------|
| `Stop` | Marks conversation turns (used with transcript fallback) |
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

- **sessions**: 会话元数据、聚合令牌/轮次/工具/子代理计数器
- **model_usage**: 按 `(session_id, model_id)` 分解的 token 追踪，独立 snapshot 检测，用于精确按模型成本计算
- **tool_calls**: 工具调用记录
- **subagent_events**: 子代理启动/停止事件

Sessions older than 30 days (no updates) are auto-cleaned. Delete `usage.db` to reset all data.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Statusline not showing | Verify `statusLine` config in `~/.claude/settings.json`, restart Claude Code |
| Cost shows `-` | `pip install pyyaml`, check `pricing.yaml` exists |
| NEXT shows `-` | Normal — no transcript in early session; will populate after first response |
| TURNS stuck at 0 | Stop hook doesn't emit turn count; falls back to transcript parsing (OK) |
| CACHE shows 0 | First call has no cache; will populate on subsequent calls |
| AGENTS always 0/0r | SubagentStart hook may not fire on your Claude Code version (SubagentStop does — verified) |
| Numbers look wrong after restart | Delete `usage.db` to reset cumulative counters |

## Compatibility

- Linux (x86_64, aarch64)
- macOS (Apple Silicon, Intel)
- Windows (WSL2)
- Windows Terminal with ANSI support

## License

MIT
