# Installation

> [安装指南](../zh/installation.md)

## Requirements

- **Python 3.11+**
- **pyyaml** (auto-installed via pip)
- **SQLite3** (stdlib, no additional installation needed)
- **ANSI-capable terminal**: Linux, macOS, Windows Terminal, or any terminal with ANSI escape support

## Quick Install

```bash
git clone https://github.com/stofancy/claude-code-statusline.git
cd claude-code-statusline
bash install.sh
```

The installer will:
1. Detect Python 3.11+
2. Create a virtual environment at `~/.claude/statusline/venv`
3. Install the package via pip
4. Output the JSON configuration to merge into `~/.claude/settings.json`

## pip Install

```bash
pip install git+https://github.com/stofancy/claude-code-statusline.git
```

Or via PyPI (if published):
```bash
pip install claude-code-statusline
```

## Post-Install Configuration

After installation, merge the hook and statusline configuration into `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event stop"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event tool"
          }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event tool"
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event subagent-start"
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/statusline/venv/bin/ccs-tracker --event subagent-stop"
          }
        ]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline/venv/bin/ccs-statusline",
    "padding": 2,
    "refreshInterval": 15
  }
}
```

Restart Claude Code (or start a new session) after merging the configuration.

## Verifying Installation

The statusline should appear within 15 seconds of starting a Claude Code session. The initial display shows:

```
ccs: waiting for session data...
```

Once a conversation begins, the two-line display activates.

## Example Configuration

See [`examples/settings.json`](../../examples/settings.json) for a complete reference configuration including environment variables for multi-provider setups (DeepSeek, Anthropic, OpenAI, Gemini).

## Platform Support

| Platform | Status |
|----------|--------|
| Linux (x86_64, aarch64) | Fully supported |
| macOS (Apple Silicon, Intel) | Fully supported |
| Windows (WSL2) | Fully supported |
| Windows Terminal | Supported (requires ANSI support) |
| Native Windows (no WSL) | Not tested |
