param(
  [switch]$SkipBackup
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$DbPath = Join-Path $BackendDir "footy.db"
$BackupScript = Join-Path $PSScriptRoot "backup_db.ps1"
$DevStackScript = Join-Path $PSScriptRoot "dev_stack.ps1"
$AssertScript = Join-Path $PSScriptRoot "assert_no_test_data.ps1"
$previousPythonIoEncoding = $env:PYTHONIOENCODING

Write-Output "Stopping local stack (if running)..."
& powershell -ExecutionPolicy Bypass -File $DevStackScript down

if ((-not $SkipBackup) -and (Test-Path $DbPath)) {
  Write-Output "Creating backup for backend/footy.db..."
  & powershell -ExecutionPolicy Bypass -File $BackupScript -DbPath "backend/footy.db"
}

if (Test-Path $DbPath) {
  Remove-Item -LiteralPath $DbPath -Force
  Write-Output "Removed: $DbPath"
}
else {
  Write-Output "No existing backend DB found at: $DbPath"
}

Write-Output "Bootstrapping from exports into backend/footy.db..."
Push-Location $BackendDir
try {
  $env:PYTHONIOENCODING = "utf-8"
  $python = "python"
  $venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
  if (Test-Path $venvPython) {
    $python = $venvPython
  }
  & $python scripts/bootstrap_from_exports.py
  if ($LASTEXITCODE -ne 0) {
    throw "bootstrap_from_exports.py failed with exit code $LASTEXITCODE"
  }
}
finally {
  if ($null -eq $previousPythonIoEncoding) {
    Remove-Item -Path "Env:PYTHONIOENCODING" -ErrorAction SilentlyContinue
  }
  else {
    Set-Item -Path "Env:PYTHONIOENCODING" -Value $previousPythonIoEncoding
  }
  Pop-Location
}

$metricsJson = & powershell -ExecutionPolicy Bypass -File $AssertScript -DbPath "backend/footy.db"
if ($LASTEXITCODE -ne 0) {
  throw "Post-bootstrap assert failed. Run 'bun run dev:data:assert-clean' for details."
}

$metrics = $metricsJson | ConvertFrom-Json

if ($metrics.products -le 0 -or $metrics.source_catalog_items -le 0) {
  throw "Bootstrap produced empty catalog (products=$($metrics.products), source_catalog_items=$($metrics.source_catalog_items))."
}

Write-Output "Bootstrap complete."
Write-Output "DB path: backend/footy.db"
Write-Output "Counts: products=$($metrics.products), source_catalog_items=$($metrics.source_catalog_items), variants=$($metrics.product_variants), images=$($metrics.product_images)"

$RootDbPath = Join-Path $RepoRoot "footy.db"
if (Test-Path $RootDbPath) {
  Write-Output "Note: root-level footy.db exists but dev stack uses backend/footy.db."
}
