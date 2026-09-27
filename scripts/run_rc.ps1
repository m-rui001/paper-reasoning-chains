param(
  [Parameter(Mandatory=$true, Position=0)]
  [string]$Mode,
  [int]$Workers = 4
)
$ErrorActionPreference = 'Stop'
if (-not $env:ARK_API_KEY) { throw 'Set ARK_API_KEY in the environment before running.' }
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
  if ($Mode -eq '--scan') {
    python scripts/pi_rc.py --scan --workers $Workers
  } else {
    python scripts/pi_rc.py --queue $Mode --workers $Workers
  }
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
