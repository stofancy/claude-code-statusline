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

# 4. Verify binaries
CCS_STATUSLINE="$VENV_DIR/bin/ccs-statusline"
CCS_TRACKER="$VENV_DIR/bin/ccs-tracker"
if [ ! -x "$CCS_STATUSLINE" ]; then
    echo -e "${RED}Error: ccs-statusline not found after install${NC}"
    exit 1
fi
echo -e "${GREEN}✓${NC} ccs-statusline → $CCS_STATUSLINE"
echo -e "${GREEN}✓${NC} ccs-tracker   → $CCS_TRACKER"

# 5. Configure Claude Code settings
CC_SETTINGS="$HOME/.claude/settings.json"
HOOKS_JSON=$(cat <<'ENDHOOKS'
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "CCS_TRACKER_PATH --event stop"
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
            "command": "CCS_TRACKER_PATH --event tool"
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
            "command": "CCS_TRACKER_PATH --event subagent-start"
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
            "command": "CCS_TRACKER_PATH --event subagent-stop"
          }
        ]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "CCS_STATUSLINE_PATH",
    "padding": 2,
    "refreshInterval": 15
  }
}
ENDHOOKS
)

HOOKS_JSON="${HOOKS_JSON//CCS_TRACKER_PATH/$CCS_TRACKER}"
HOOKS_JSON="${HOOKS_JSON//CCS_STATUSLINE_PATH/$CCS_STATUSLINE}"

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
