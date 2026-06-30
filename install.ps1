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
& $venvPip install -q -e $packageDir
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

# 4. Auto-merge hooks + statusLine into ~/.claude/settings.json.
#    Claude Code invokes hooks via bash; backslash paths get mangled, so we use
#    forward-slash ~ paths that bash expands correctly on Windows.
$settingsPath = Join-Path $HOME '.claude\settings.json'
$base = '~/.claude/statusline/venv/Scripts'

# Load existing settings (PS7 -AsHashtable gives a mutable, recursive hashtable)
# or start fresh. Anything that won't parse is treated as empty so we never crash.
if (Test-Path $settingsPath) {
    try {
        $settings = Get-Content $settingsPath -Raw | ConvertFrom-Json -AsHashtable
    } catch {
        Write-Red "Warning: existing settings.json is not valid JSON; backing it up and starting fresh."
        $settings = $null
    }
} else {
    New-Item -ItemType Directory -Force (Split-Path $settingsPath) | Out-Null
}
if (-not $settings) { $settings = @{} }
if (-not $settings.ContainsKey('hooks') -or $settings['hooks'] -isnot [hashtable]) {
    $settings['hooks'] = @{}
}

# Append our hook for an event while preserving unrelated hooks and staying
# idempotent: drop any prior entry that referenced our ccs- binaries first.
function Merge-CcsHook([hashtable]$cfg, [string]$event, [string]$cmd) {
    $kept = @()
    if ($cfg['hooks'].ContainsKey($event)) {
        $kept = @($cfg['hooks'][$event] | Where-Object {
            -not ($_.hooks | Where-Object { $_.command -match 'ccs-tracker|ccs-statusline' })
        })
    }
    $kept += @{ matcher = ''; hooks = @(@{ type = 'command'; command = $cmd }) }
    $cfg['hooks'][$event] = $kept
}

Merge-CcsHook $settings 'Stop'          "$base/ccs-tracker.exe --event stop"
Merge-CcsHook $settings 'PostToolUse'   "$base/ccs-tracker.exe --event tool"
Merge-CcsHook $settings 'SubagentStart' "$base/ccs-tracker.exe --event subagent-start"
Merge-CcsHook $settings 'SubagentStop'  "$base/ccs-tracker.exe --event subagent-stop"

$settings['statusLine'] = @{
    type            = 'command'
    command         = "$base/ccs-statusline.exe"
    padding         = 2
    refreshInterval = 15
}

# Back up before overwriting, then write UTF-8 without BOM (Claude Code's JSON
# parser chokes on a BOM).
if (Test-Path $settingsPath) {
    Copy-Item $settingsPath "$settingsPath.bak" -Force
    Write-Cyan "→ Backed up existing settings to $settingsPath.bak"
}
$json = $settings | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($settingsPath, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ''
Write-Green "✓ Merged hooks + statusLine into $settingsPath"
Write-Green '✓ Installation complete.'
Write-Host ''
Write-Bold 'Optional environment variables:'
Write-Host '  CCS_LANG=zh         # 中文界面'
Write-Host '  CCS_CURRENCY=USD    # display in USD (or CNY / EUR / GBP / JPY ...)'
Write-Host '  CCS_DEBUG=1         # write debug log to ~/.claude/statusline/debug.log'
Write-Host ''
Write-Bold 'Next step:'
Write-Host '  Restart Claude Code (or start a new session) for the statusline to take effect.'
