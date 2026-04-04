param(
  [switch]$SkipBackup
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$DbPath = Join-Path $BackendDir "footy.db"
$BackupScript = Join-Path $PSScriptRoot "backup_db.ps1"
$DevStackScript = Join-Path $PSScriptRoot "dev_stack.ps1"

Write-Output "Stopping local stack (if running)..."
& powershell -ExecutionPolicy Bypass -File $DevStackScript down

if (-not (Test-Path $DbPath)) {
  throw "Working DB not found: backend/footy.db"
}

if (-not $SkipBackup) {
  Write-Output "Creating backup for backend/footy.db..."
  & powershell -ExecutionPolicy Bypass -File $BackupScript -DbPath "backend/footy.db"
}

Write-Output "Repairing product media in backend/footy.db..."
Push-Location $BackendDir
try {
  python scripts/repair_product_media.py
  if ($LASTEXITCODE -ne 0) {
    throw "repair_product_media.py failed with exit code $LASTEXITCODE"
  }
}
finally {
  Pop-Location
}

Write-Output "Media repair complete. Start stack with: bun run dev:all"
