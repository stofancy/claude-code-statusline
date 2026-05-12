# Claude Code Statusline — Windows installer (PowerShell 7+)
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1
#    or: pwsh -File install.ps1

$ErrorActionPreference = 'Stop'

function Write-Cyan($msg)  { Write-Host $msg -ForegroundColor Cyan }
function Write-Green($msg) { Write-Host $msg -ForegroundColor Green }
function Write-Red($msg)   { Write-Host $msg -ForegroundColor Red }
function Write-Bold($msg)  { Write-Host $msg -ForegroundColor White }

Write-Cyan '╔══════════════════════════════════════════╗'
Write-Cyan '║  Claude Code Statusline Installer (Win)  ║'
Write-Cyan '╚══════════════════════════════════════════╝'
Write-Host ''

# 1. Locate Python 3.11+
$python = $null
foreach ($candidate in @('py -3.12', 'py -3.11', 'py -3', 'python', 'python3')) {
    try {
        $parts = $candidate -split '\s+', 2
        $exe = $parts[0]
        $rest = if ($parts.Length -gt 1) { $parts[1] } else { '' }
        $verRaw = & $exe $rest -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $verRaw) {
            $ver = [version]$verRaw.Trim()
            if ($ver.Major -ge 3 -and $ver.Minor -ge 11) {
                $python = $candidate
                Write-Green "✓ Found Python $verRaw via '$python'"
                break
            }
        }
    } catch {}
}

if (-not $python) {
    Write-Red 'Error: Python 3.11+ required but not found.'
    exit 1
}

# 2. Create venv at ~/.claude/statusline/venv
$venvDir = Join-Path $HOME '.claude\statusline\venv'
if (-not (Test-Path $venvDir)) {
    Write-Cyan "→ Creating virtual environment at $venvDir ..."
    $pythonParts = $python -split '\s+', 2
    $pyExe = $pythonParts[0]
    $pyRest = if ($pythonParts.Length -gt 1) { $pythonParts[1] } else { '' }
    if ($pyRest) {
        & $pyExe $pyRest -m venv $venvDir
    } else {
        & $pyExe -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Red 'Error: failed to create venv'
        exit 1
    }
}

$venvPy  = Join-Path $venvDir 'Scripts\python.exe'
$venvPip = Join-Path $venvDir 'Scripts\pip.exe'

# 3. Install the package from the local source dir
$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Cyan '→ Installing claude-code-statusline ...'
& $venvPip install -q --upgrade pip | Out-Null
& $venvPip install -q $packageDir
if ($LASTEXITCODE -ne 0) {
    Write-Red 'Error: pip install failed'
    exit 1
}

$ccsStatusline = Join-Path $venvDir 'Scripts\ccs-statusline.exe'
$ccsTracker    = Join-Path $venvDir 'Scripts\ccs-tracker.exe'

if (-not (Test-Path $ccsStatusline)) {
    Write-Red "Error: ccs-statusline.exe not found at $ccsStatusline"
    exit 1
}

Write-Green "✓ ccs-statusline → $ccsStatusline"
Write-Green "✓ ccs-tracker   → $ccsTracker"

# 4. Print suggested settings.json snippet using bash-friendly ~ paths.
#    Claude Code invokes hooks via bash; backslash paths get mangled, so the
#    snippet uses forward-slash ~ paths that bash expands correctly on Windows.
$snippet = @'
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/statusline/venv/Scripts/ccs-tracker.exe --event stop" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/statusline/venv/Scripts/ccs-tracker.exe --event tool" }
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/statusline/venv/Scripts/ccs-tracker.exe --event tool" }
        ]
      }
    ],
    "SubagentStart": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/statusline/venv/Scripts/ccs-tracker.exe --event subagent-start" }
        ]
      }
    ],
    "SubagentStop": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "~/.claude/statusline/venv/Scripts/ccs-tracker.exe --event subagent-stop" }
        ]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline/venv/Scripts/ccs-statusline.exe",
    "padding": 2,
    "refreshInterval": 15
  }
}
'@

Write-Host ''
Write-Cyan '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
Write-Bold 'Merge the following into ~/.claude/settings.json:'
Write-Cyan '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
Write-Host ''
Write-Host $snippet
Write-Host ''
Write-Green '✓ Installation complete.'
Write-Host ''
Write-Bold 'Optional environment variables:'
Write-Host '  CCS_LANG=zh         # 中文界面'
Write-Host '  CCS_CURRENCY=USD    # display in USD (or CNY / EUR / GBP / JPY ...)'
Write-Host '  CCS_DEBUG=1         # write debug log to ~/.claude/statusline/debug.log'
Write-Host ''
Write-Bold 'Next step:'
Write-Host '  Restart Claude Code (or start a new session) for the statusline to take effect.'
