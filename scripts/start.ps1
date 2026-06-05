param(
  [switch]$SkipInstall,
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 3000
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$RootEnvExample = Join-Path $RepoRoot ".env.example"
$BackendEnv = Join-Path $BackendDir ".env"
$FrontendEnv = Join-Path $FrontendDir ".env.local"
$BackendPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
$DevStackScript = Join-Path $PSScriptRoot "dev_stack.ps1"

function Resolve-SystemPython {
  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python) {
    return "python"
  }

  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    return "py -3"
  }

  throw "Python was not found. Install Python 3.12+ and run this command again."
}

function Ensure-EnvFiles {
  if (-not (Test-Path $BackendEnv)) {
    Copy-Item $RootEnvExample $BackendEnv
    Write-Output "Created backend/.env"
  }

  if (-not (Test-Path $FrontendEnv)) {
    "NEXT_PUBLIC_API_BASE_URL=http://localhost:$BackendPort" |
      Set-Content -Path $FrontendEnv -Encoding UTF8
    Write-Output "Created frontend/.env.local"
    return
  }

  $frontendEnvContent = Get-Content $FrontendEnv -Raw
  $expectedApiBaseUrl = "NEXT_PUBLIC_API_BASE_URL=http://localhost:$BackendPort"
  if ($frontendEnvContent -match "(?m)^NEXT_PUBLIC_API_BASE_URL=http://localhost:\d+\s*$" -and
      $frontendEnvContent -notmatch "(?m)^$([regex]::Escape($expectedApiBaseUrl))\s*$") {
    $updatedContent = $frontendEnvContent -replace "(?m)^NEXT_PUBLIC_API_BASE_URL=http://localhost:\d+\s*$", $expectedApiBaseUrl
    Set-Content -Path $FrontendEnv -Value $updatedContent -Encoding UTF8
    Write-Output "Updated frontend/.env.local API URL to http://localhost:$BackendPort"
  }
}

function Ensure-Backend {
  if (-not (Test-Path $BackendPython)) {
    $systemPython = Resolve-SystemPython
    Write-Output "Creating backend virtual environment..."
    if ($systemPython -eq "py -3") {
      & py -3 -m venv (Join-Path $BackendDir ".venv")
    }
    else {
      & python -m venv (Join-Path $BackendDir ".venv")
    }
  }

  if ($SkipInstall) {
    return
  }

  Write-Output "Installing backend dependencies..."
  Push-Location $BackendDir
  try {
    & $BackendPython -m pip install -e ".[dev]" --quiet
  }
  finally {
    Pop-Location
  }
}

function Ensure-Frontend {
  if ($SkipInstall) {
    return
  }

  if (-not (Get-Command bun -ErrorAction SilentlyContinue)) {
    throw "Bun was not found. Install Bun and run this command again."
  }

  $nextCli = Join-Path $FrontendDir "node_modules\next\dist\bin\next"
  if (Test-Path $nextCli) {
    return
  }

  Write-Output "Installing frontend dependencies with Bun..."
  Push-Location $FrontendDir
  try {
    & bun install
  }
  finally {
    Pop-Location
  }
}

Ensure-EnvFiles
Ensure-Backend
Ensure-Frontend

& powershell -ExecutionPolicy Bypass -File $DevStackScript run `
  -BackendPort $BackendPort `
  -FrontendPort $FrontendPort
