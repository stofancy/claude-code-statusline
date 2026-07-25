#!/usr/bin/env bash
set -euo pipefail

# Codex Statusline — One-command installer
# Usage: bash install-codex.sh

RED='\033[31m'
GREEN='\033[32m'
CYAN='\033[36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║  Codex Faux Statusline Installer        ║${NC}"
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
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
VENV_DIR="$CODEX_HOME_DIR/statusline/venv"
create_venv() {
    local extra_flag="${1:-}"
    if [ -n "$extra_flag" ]; then
        "$PYTHON" -m venv "$extra_flag" "$VENV_DIR"
    else
        "$PYTHON" -m venv "$VENV_DIR"
    fi
}
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${CYAN}→${NC} Creating virtual environment at $VENV_DIR ..."
    create_venv 2>/dev/null || {
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
echo -e "${CYAN}→${NC} Installing claude-code-statusline with Codex support ..."
PACKAGE_DIR="$(cd "$(dirname "$0")" && pwd)"
install_offline_fallback() {
    echo -e "${CYAN}→${NC} pip editable install failed; using local src fallback ..."
    if ! "$VENV_DIR/bin/python" -c "import yaml" 2>/dev/null; then
        if "$PYTHON" -c "import yaml" 2>/dev/null; then
            echo -e "${CYAN}→${NC} Recreating venv with system site packages for local PyYAML ..."
            rm -rf "$VENV_DIR"
            mkdir -p "$(dirname "$VENV_DIR")"
            "$PYTHON" -m venv --system-site-packages "$VENV_DIR"
        fi
    fi
    "$VENV_DIR/bin/python" -c "import yaml" 2>/dev/null || {
        echo -e "${RED}Error: offline fallback requires PyYAML in the venv.${NC}"
        echo "Run again with network access, or install PyYAML into $VENV_DIR first."
        exit 1
    }
    SITE_PACKAGES=$("$VENV_DIR/bin/python" -c "import sysconfig; print(sysconfig.get_path('purelib'))")
    mkdir -p "$SITE_PACKAGES"
    printf '%s\n' "$PACKAGE_DIR/src" > "$SITE_PACKAGES/claude_code_statusline_local.pth"
    cat > "$VENV_DIR/bin/codex-statusline" <<EOFWRAP
#!$VENV_DIR/bin/python
import sys
from ccs.codex_statusline import main
if __name__ == '__main__':
    sys.exit(main())
EOFWRAP
    chmod +x "$VENV_DIR/bin/codex-statusline"
    if [ ! -x "$VENV_DIR/bin/pip" ] && [ -x "$VENV_DIR/bin/pip3" ]; then
        ln -sf pip3 "$VENV_DIR/bin/pip"
    fi
}

if [ -f "$PACKAGE_DIR/pyproject.toml" ]; then
    "$VENV_DIR/bin/pip" install -q -e "$PACKAGE_DIR" || install_offline_fallback
else
    "$VENV_DIR/bin/pip" install -q git+https://github.com/stofancy/claude-code-statusline.git || {
        "$VENV_DIR/bin/pip" install -q claude-code-statusline || install_offline_fallback
    }
fi
echo -e "${GREEN}✓${NC} Package installed"

# 4. Verify package is importable
CCS_PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$CCS_PYTHON" ]; then
    echo -e "${RED}Error: venv python not found${NC}"
    exit 1
fi
"$CCS_PYTHON" -c "import ccs.codex_statusline, ccs.codex_transcript" || {
    echo -e "${RED}Error: Codex statusline modules not importable${NC}"
    exit 1
}
echo -e "${GREEN}✓${NC} ccs.codex_statusline → import OK"
echo -e "${GREEN}✓${NC} ccs.codex_transcript → import OK"

# 5. Configure Codex Stop hook. Use python -m so editable installs hot-load changes.
CODEX_CONFIG="$CODEX_HOME_DIR/config.toml"
CODEX_STATUSLINE_CMD="\"$CCS_PYTHON\" -m ccs.codex_statusline"
mkdir -p "$(dirname "$CODEX_CONFIG")"
touch "$CODEX_CONFIG"

if grep -Fq "ccs.codex_statusline" "$CODEX_CONFIG"; then
    echo -e "${GREEN}✓${NC} Codex Stop hook already configured in $CODEX_CONFIG"
else
    cat >> "$CODEX_CONFIG" <<EOFHOOK

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = '${CODEX_STATUSLINE_CMD}'
timeout = 10
EOFHOOK
    echo -e "${GREEN}✓${NC} Added Codex Stop hook to $CODEX_CONFIG"
fi

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}Codex hook configuration:${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
cat <<EOF

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = '${CODEX_STATUSLINE_CMD}'
timeout = 10

EOF

echo -e "${GREEN}✓${NC} Installation complete."
echo ""
echo -e "${BOLD}Next step:${NC} Restart Codex and review/trust the hook with /hooks if prompted."
