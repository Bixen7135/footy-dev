param(
  [int]$BackendPort = 8100,
  [int]$FrontendPort = 3100
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunDir = Join-Path $RepoRoot ".run"
$SmokeRunDir = Join-Path $RunDir "smoke-isolated"
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$SmokeScript = Join-Path $PSScriptRoot "run_local_smoke.ps1"
$DevStackScript = Join-Path $PSScriptRoot "dev_stack.ps1"
$SmokeDbPath = Join-Path $BackendDir "footy-smoke.db"

$BackendUrl = "http://localhost:$BackendPort"
$FrontendUrl = "http://localhost:$FrontendPort"

$BackendOut = Join-Path $SmokeRunDir "backend.log"
$BackendErr = Join-Path $SmokeRunDir "backend.err.log"
$FrontendOut = Join-Path $SmokeRunDir "frontend.log"
$FrontendErr = Join-Path $SmokeRunDir "frontend.err.log"

New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
New-Item -ItemType Directory -Path $SmokeRunDir -Force | Out-Null

function Resolve-BackendPython {
  $venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
  if (Test-Path $venvPython) {
    return $venvPython
  }
  return "python"
}

function Get-ProcessSafe([int]$processId) {
  try {
    return Get-Process -Id $processId -ErrorAction Stop
  }
  catch {
    return $null
  }
}

function Test-PortListening([int]$port) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  return $null -ne $conn
}

function Stop-ListenersOnPort([int]$port, [string]$label) {
  $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($ownerPid in $listeners) {
    if (-not $ownerPid -or $ownerPid -eq 0) {
      continue
    }
    $proc = Get-ProcessSafe -processId $ownerPid
    if ($proc) {
      Stop-Process -Id $ownerPid -Force
      Write-Output "Stopped leftover $label listener (PID $ownerPid) on port $port"
    }
  }
}

function Wait-Url([string]$url, [int]$timeoutSeconds = 60) {
  for ($i = 0; $i -lt $timeoutSeconds; $i++) {
    try {
      Invoke-WebRequest $url -UseBasicParsing | Out-Null
      return $true
    }
    catch {
      Start-Sleep -Seconds 1
    }
  }
  return $false
}

function Restore-EnvVar([string]$name, $previousValue) {
  if ($null -eq $previousValue) {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
  }
  else {
    Set-Item -Path "Env:$name" -Value $previousValue
  }
}

if (Test-PortListening -port $BackendPort) {
  throw "Backend smoke port $BackendPort is already in use."
}
if (Test-PortListening -port $FrontendPort) {
  throw "Frontend smoke port $FrontendPort is already in use."
}

$python = Resolve-BackendPython
$nextCli = Join-Path $FrontendDir "node_modules\next\dist\bin\next"
if (-not (Test-Path $nextCli)) {
  throw "Next CLI not found at $nextCli. Run 'bun install' in frontend first."
}

Remove-Item $BackendOut, $BackendErr, $FrontendOut, $FrontendErr -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $SmokeDbPath -Force -ErrorAction SilentlyContinue

$previousDatabaseUrl = $env:FOOTY_DATABASE_URL
$previousCorsOrigins = $env:FOOTY_CORS_ORIGINS
$previousFrontendApiUrl = $env:NEXT_PUBLIC_API_BASE_URL
$previousPlaywrightBaseUrl = $env:PLAYWRIGHT_BASE_URL
$previousE2EApiBaseUrl = $env:E2E_API_BASE_URL

$backend = $null
$frontend = $null

try {
  # Next.js dev mode keeps a project-level lock. Stop shared stack to avoid lock conflicts.
  Write-Output "Stopping shared dev stack (if running)..."
  & powershell -ExecutionPolicy Bypass -File $DevStackScript down

  $env:FOOTY_DATABASE_URL = "sqlite:///./footy-smoke.db"
  $env:FOOTY_CORS_ORIGINS = $FrontendUrl
  $backend = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "localhost", "--port", "$BackendPort") `
    -WorkingDirectory $BackendDir `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $BackendOut `
    -RedirectStandardError $BackendErr

  $env:NEXT_PUBLIC_API_BASE_URL = $BackendUrl
  $frontend = Start-Process -FilePath "node" `
    -ArgumentList @($nextCli, "dev", "--port", "$FrontendPort", "--hostname", "localhost") `
    -WorkingDirectory $FrontendDir `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $FrontendOut `
    -RedirectStandardError $FrontendErr

  if (-not (Wait-Url "$BackendUrl/health" 120)) {
    throw "Backend isolated smoke stack is not healthy in time. Check $BackendErr"
  }
  if (-not (Wait-Url $FrontendUrl 180)) {
    throw "Frontend isolated smoke stack is not ready in time. Check $FrontendErr"
  }

  $env:PLAYWRIGHT_BASE_URL = $FrontendUrl
  $env:E2E_API_BASE_URL = $BackendUrl

  Write-Output "Running isolated smoke against $BackendUrl and $FrontendUrl"
  & powershell -ExecutionPolicy Bypass -File $SmokeScript -BackendUrl $BackendUrl -FrontendUrl $FrontendUrl
  if ($LASTEXITCODE -ne 0) {
    throw "Isolated smoke failed with exit code $LASTEXITCODE"
  }

  Write-Output "Isolated smoke completed."
}
finally {
  if ($frontend -and (Get-ProcessSafe -processId $frontend.Id)) {
    Stop-Process -Id $frontend.Id -Force
  }
  if ($backend -and (Get-ProcessSafe -processId $backend.Id)) {
    Stop-Process -Id $backend.Id -Force
  }

  Stop-ListenersOnPort -port $FrontendPort -label "frontend"
  Stop-ListenersOnPort -port $BackendPort -label "backend"

  Remove-Item -LiteralPath $SmokeDbPath -Force -ErrorAction SilentlyContinue
  if (Test-Path $SmokeDbPath) {
    Write-Output "Warning: isolated DB still exists at $SmokeDbPath"
  }
  else {
    Write-Output "Removed isolated DB: backend/footy-smoke.db"
  }

  Restore-EnvVar -name "FOOTY_DATABASE_URL" -previousValue $previousDatabaseUrl
  Restore-EnvVar -name "FOOTY_CORS_ORIGINS" -previousValue $previousCorsOrigins
  Restore-EnvVar -name "NEXT_PUBLIC_API_BASE_URL" -previousValue $previousFrontendApiUrl
  Restore-EnvVar -name "PLAYWRIGHT_BASE_URL" -previousValue $previousPlaywrightBaseUrl
  Restore-EnvVar -name "E2E_API_BASE_URL" -previousValue $previousE2EApiBaseUrl
}
