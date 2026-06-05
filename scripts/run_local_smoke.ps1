param(
  [string]$BackendUrl = "http://localhost:8000",
  [string]$FrontendUrl = "http://localhost:3000"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($env:FOOTY_DATABASE_URL)) {
  throw "FOOTY_DATABASE_URL is not set. Run smoke via isolated runner: bun run dev:all:smoke"
}

$dbUrl = $env:FOOTY_DATABASE_URL.ToLowerInvariant()
if ($dbUrl -notlike "*footy-smoke.db*") {
  throw "Refusing to run smoke against non-isolated DB ($($env:FOOTY_DATABASE_URL)). Use backend/footy-smoke.db only."
}

Write-Output "Seeding demo data..."
Push-Location backend
$python = "python"
$venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
  $python = $venvPython
}
& $python scripts/seed_demo_data.py
Pop-Location

Write-Output "Checking backend health..."
Invoke-WebRequest "$BackendUrl/health" -UseBasicParsing | Out-Null
Invoke-WebRequest "$BackendUrl/health/ready" -UseBasicParsing | Out-Null

Write-Output "Checking frontend..."
Invoke-WebRequest "$FrontendUrl" -UseBasicParsing | Out-Null

Write-Output "Running Playwright smoke tests..."
Push-Location frontend
$env:PLAYWRIGHT_BASE_URL = $FrontendUrl
bun run e2e
Pop-Location
if ($LASTEXITCODE -ne 0) {
  throw "Playwright smoke tests failed with exit code $LASTEXITCODE"
}

Write-Output "Local smoke completed."
