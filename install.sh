#!/usr/bin/env bash
set -euo pipefail

# Claude Code Statusline — One-command installer
# Usage: curl -fsSL <url>/install.sh | bash
#    or: bash install.sh

RED='\033[31m'
GREEN='\033[32m'
CYAN='\033[36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║  Claude Code Statusline Installer       ║${NC}"
echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# 1. Check Python
PYTHON=""
for candidate in python3 python3.12 python3.11; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${RED}Error: Python 3.11+ required but not found.${NC}"
    exit 1
fi
echo -e "${GREEN}✓${NC} Found $PYTHON ($("$PYTHON" --version))"

# 2. Create venv if not exists
VENV_DIR="$HOME/.claude/statusline/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${CYAN}→${NC} Creating virtual environment at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR" 2>/dev/null || {
        echo -e "${CYAN}→${NC} ensurepip not available, trying without pip ..."
        "$PYTHON" -m venv --without-pip "$VENV_DIR" 2>/dev/null || {
            echo -e "${CYAN}→${NC} Trying virtualenv ..."
            pip3 install --user virtualenv 2>/dev/null || true
            "$PYTHON" -m virtualenv "$VENV_DIR" 2>/dev/null || true
        }
        if [ -f "$VENV_DIR/bin/python3" ]; then
            curl -sS https://bootstrap.pypa.io/get-pip.py | "$VENV_DIR/bin/python3" 2>/dev/null || true
        fi
    }
fi

# 3. Install package
echo -e "${CYAN}→${NC} Installing claude-code-statusline ..."
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$PACKAGE_DIR/pyproject.toml" ]; then
    "$VENV_DIR/bin/pip" install -q -e "$PACKAGE_DIR"
else
    "$VENV_DIR/bin/pip" install -q git+https://github.com/stofancy/claude-code-statusline.git || {
        "$VENV_DIR/bin/pip" install -q claude-code-statusline
    }
fi
echo -e "${GREEN}✓${NC} Package installed"

# 4. Verify package is importable
CCS_PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$CCS_PYTHON" ]; then
    echo -e "${RED}Error: venv python not found${NC}"
    exit 1
fi
"$CCS_PYTHON" -c "import ccs.statusline, ccs.tracker" || {
    echo -e "${RED}Error: ccs modules not importable${NC}"
    exit 1
}
echo -e "${GREEN}✓${NC} ccs.statusline  → import OK"
echo -e "${GREEN}✓${NC} ccs.tracker     → import OK"

# 5. Configure Claude Code settings — use python -m form so edits are hot-loaded
CC_SETTINGS="$HOME/.claude/settings.json"
CCS_TRACKER_CMD="$CCS_PYTHON -m ccs.tracker"
CCS_STATUSLINE_CMD="$CCS_PYTHON -m ccs.statusline"
HOOKS_JSON=$(cat <<'ENDHOOKS'
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "CCS_TRACKER_CMD --event stop"
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
            "command": "CCS_TRACKER_CMD --event tool"
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
            "command": "CCS_TRACKER_CMD --event subagent-start"
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
            "command": "CCS_TRACKER_CMD --event subagent-stop"
          }
        ]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "CCS_STATUSLINE_CMD",
    "padding": 2,
    "refreshInterval": 15
  }
}
ENDHOOKS
)

HOOKS_JSON="${HOOKS_JSON//CCS_TRACKER_CMD/$CCS_TRACKER_CMD}"
HOOKS_JSON="${HOOKS_JSON//CCS_STATUSLINE_CMD/$CCS_STATUSLINE_CMD}"

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}Add the following to ~/.claude/settings.json:${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "$HOOKS_JSON"
echo ""
echo -e "${GREEN}✓${NC} Installation complete."
echo ""
echo -e "${BOLD}Next step:${NC} Merge the above JSON into ${CYAN}~/.claude/settings.json${NC}"
echo "Then restart Claude Code (or start a new session)."
