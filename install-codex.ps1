# Codex Statusline — Windows installer (PowerShell 7+)
# Usage: pwsh -File install-codex.ps1

$ErrorActionPreference = 'Stop'

function Write-Cyan($msg)  { Write-Host $msg -ForegroundColor Cyan }
function Write-Green($msg) { Write-Host $msg -ForegroundColor Green }
function Write-Red($msg)   { Write-Host $msg -ForegroundColor Red }
function Write-Bold($msg)  { Write-Host $msg -ForegroundColor White }

Write-Cyan '╔══════════════════════════════════════════╗'
Write-Cyan '║  Codex Faux Statusline Installer (Win)   ║'
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

# 2. Create venv at $CODEX_HOME/statusline/venv or ~/.codex/statusline/venv
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$venvDir = Join-Path $codexHome 'statusline\venv'
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
Write-Cyan '→ Installing claude-code-statusline with Codex support ...'
& $venvPip install -q --upgrade pip | Out-Null
& $venvPip install -q -e $packageDir
if ($LASTEXITCODE -ne 0) {
    Write-Cyan '→ pip editable install failed; using local src fallback ...'
    & $venvPy -c 'import yaml' 2>$null
    if ($LASTEXITCODE -ne 0) {
        $pythonParts = $python -split '\s+', 2
        $pyExe = $pythonParts[0]
        $pyRest = if ($pythonParts.Length -gt 1) { $pythonParts[1] } else { '' }
        if ($pyRest) { & $pyExe $pyRest -c 'import yaml' 2>$null } else { & $pyExe -c 'import yaml' 2>$null }
        if ($LASTEXITCODE -eq 0) {
            Write-Cyan '→ Recreating venv with system site packages for local PyYAML ...'
            Remove-Item -Recurse -Force $venvDir
            if ($pyRest) { & $pyExe $pyRest -m venv --system-site-packages $venvDir } else { & $pyExe -m venv --system-site-packages $venvDir }
        }
    }
    & $venvPy -c 'import yaml' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Red "Error: offline fallback requires PyYAML in the venv. Run again with network access, or install PyYAML into $venvDir first."
        exit 1
    }
    $sitePackages = & $venvPy -c "import sysconfig; print(sysconfig.get_path('purelib'))"
    if (-not (Test-Path $sitePackages)) { New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null }
    Set-Content -Path (Join-Path $sitePackages 'claude_code_statusline_local.pth') -Value (Join-Path $packageDir 'src') -Encoding UTF8
    $wrapper = @"
#!$venvPy
import sys
from ccs.codex_statusline import main
if __name__ == '__main__':
    sys.exit(main())
"@
    Set-Content -Path (Join-Path $venvDir 'Scripts\codex-statusline.py') -Value $wrapper -Encoding UTF8
}

# 4. Verify importability
& $venvPy -c 'import ccs.codex_statusline, ccs.codex_transcript'
if ($LASTEXITCODE -ne 0) {
    Write-Red 'Error: Codex statusline modules not importable'
    exit 1
}
Write-Green '✓ ccs.codex_statusline → import OK'
Write-Green '✓ ccs.codex_transcript → import OK'

# 5. Configure Codex Stop hook. Codex command strings are parsed by a shell;
# use forward slashes and quote the paths to survive spaces.
$venvPyForToml = $venvPy.Replace('\', '/')
$codexConfig = Join-Path $codexHome 'config.toml'
$command = '"' + $venvPyForToml + '" -m ccs.codex_statusline'

$snippet = @"
[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = '$command'
timeout = 10
"@

if (-not (Test-Path $codexHome)) { New-Item -ItemType Directory -Force -Path $codexHome | Out-Null }
if (-not (Test-Path $codexConfig)) { New-Item -ItemType File -Force -Path $codexConfig | Out-Null }
$content = Get-Content -Raw -Path $codexConfig
if ($content -match 'ccs\.codex_statusline') {
    Write-Green "✓ Codex Stop hook already configured in $codexConfig"
} else {
    Add-Content -Path $codexConfig -Value ("`n" + $snippet) -Encoding UTF8
    Write-Green "✓ Added Codex Stop hook to $codexConfig"
}

Write-Host ''
Write-Cyan '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
Write-Bold 'Codex hook configuration:'
Write-Cyan '━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━'
Write-Host ''
Write-Host $snippet
Write-Host ''
Write-Green '✓ Installation complete.'
Write-Host ''
Write-Bold 'Next step:'
Write-Host '  Restart Codex and review/trust the hook with /hooks if prompted.'
