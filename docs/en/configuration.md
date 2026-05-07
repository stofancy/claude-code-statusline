# Configuration

> [配置参考](../zh/configuration.md)

## Hook Events

Claude Code hooks are the mechanism for tracking session events. The following hooks must be registered in `~/.claude/settings.json`:

| Hook | Event Type | Purpose |
|------|------------|---------|
| `Stop` | `stop` | Turn boundary marker — ensures session exists in DB |
| `PostToolUse` | `tool` | Counts tool calls |
| `PostToolUseFailure` | `tool` | Counts failed tool calls (same event type) |
| `SubagentStart` | `subagent-start` | Tracks subagent spawn |
| `SubagentStop` | `subagent-stop` | Tracks subagent completion |

## Statusline Configuration

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline/venv/bin/ccs-statusline",
    "padding": 2,
    "refreshInterval": 15
  }
}
```

| Field | Description |
|-------|-------------|
| `type` | Must be `"command"` |
| `command` | Path to the `ccs-statusline` binary (auto-detected by installer) |
| `padding` | Vertical padding around statusline. Default `2`. |
| `refreshInterval` | Update interval in seconds. Default `15`. Lower values increase CPU usage. |

## Pricing Override

Default pricing is baked into `src/ccs/pricing.yaml`. To override:

1. Create `~/.claude/statusline/pricing.yaml`
2. Copy the structure from the built-in file
3. Modify prices as needed
4. Changes take effect on next statusline/tracker call (no restart needed)

Priority: user file → built-in file → fallback defaults.

All prices are in **CNY (¥) per million tokens**. To use USD:
```yaml
default_currency: USD
```

### Built-in Pricing Table

| Provider | Model | Input | Cache Hit | Cache Write | Output |
|----------|-------|-------|-----------|-------------|--------|
| DeepSeek | V4-Pro (2.5× off) | ¥3.00 | ¥0.025 | — | ¥6.00 |
| DeepSeek | V4-Flash | ¥1.00 | ¥0.02 | — | ¥2.00 |
| DeepSeek | R1 | ¥4.00 | ¥0.25 | — | ¥16.00 |
| Anthropic | Opus 4.7 | ¥107.73 | ¥10.77 | ¥215.46 | ¥538.65 |
| Anthropic | Sonnet 4.6 | ¥21.55 | ¥2.15 | ¥43.09 | ¥107.73 |
| Anthropic | Haiku 4.5 | ¥7.18 | ¥0.72 | ¥14.37 | ¥35.91 |
| OpenAI | GPT-4o | ¥17.95 | ¥8.98 | — | ¥71.80 |
| OpenAI | GPT-4o-mini | ¥1.08 | ¥0.54 | — | ¥4.31 |
| OpenAI | GPT-5 | ¥8.98 | ¥1.80 | — | ¥71.80 |
| Google | Gemini 2.5 Pro | ¥8.98 | ¥0.89 | — | ¥35.91 |
| Google | Gemini 2.5 Flash | ¥1.08 | ¥0.11 | — | ¥4.31 |

**Note**: DeepSeek V4-Pro 2.5× discount valid until 2026/05/31 23:59 Beijing time. Update `pricing.yaml` when the promotion ends.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `CCS_DEBUG=1` | Write raw hook JSON data to `~/.claude/statusline/debug.log` for troubleshooting |

## Multi-Provider Setup

To use DeepSeek (or other providers) as the API backend while keeping statusline tracking accurate:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash"
  }
}
```

The statusline detects the model from Claude Code's session JSON and prices it according to your pricing table — no additional configuration needed.

## Database

- **Location**: `~/.claude/statusline/usage.db`
- **Engine**: SQLite with WAL mode
- **Lifetime**: Sessions older than 30 days are auto-cleaned
- **Reset**: Delete the `.db` file to reset all accumulated data

### Tables

| Table | Contents |
|-------|----------|
| `sessions` | Session metadata, cumulative token counters, turn count, tool/subagent counters |
| `model_usage` | Per-model `(session_id, model_id)` token breakdown for accurate cost calculation |
| `tool_calls` | Per-turn tool call records |
| `subagent_events` | Subagent start/stop events |
