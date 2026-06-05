param(
  [ValidateSet("run", "up", "down", "status", "smoke", "logs")]
  [string]$Action = "up",
  [int]$BackendPort = 8000,
  [int]$FrontendPort = 3000
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunDir = Join-Path $RepoRoot ".run"
$StatePath = Join-Path $RunDir "dev-stack.json"
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$SmokeScript = Join-Path $PSScriptRoot "run_local_smoke_isolated.ps1"

New-Item -ItemType Directory -Path $RunDir -Force | Out-Null

function Resolve-BackendPython {
  $venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
  if (Test-Path $venvPython) {
    return $venvPython
  }
  return "python"
}

function Resolve-FrontendCommand([int]$port) {
  # On Windows, "bun run dev" + Next.js may fail with "spawn EPERM" when started detached.
  # Use Next CLI through node for reliable background startup in this stack script.
  $nextCli = Join-Path $FrontendDir "node_modules\next\dist\bin\next"
  if (-not (Test-Path $nextCli)) {
    throw "Next CLI not found at $nextCli. Run 'bun install' in frontend first."
  }

  return @{
    filePath = "node"
    args = @($nextCli, "dev", "--webpack", "--port", "$port", "--hostname", "localhost")
  }
}

function Read-State {
  if (-not (Test-Path $StatePath)) {
    return $null
  }
  return Get-Content $StatePath -Raw | ConvertFrom-Json
}

function Write-State($state) {
  $state | ConvertTo-Json | Set-Content -Path $StatePath -Encoding UTF8
}

function Remove-State {
  if (Test-Path $StatePath) {
    Remove-Item $StatePath -Force
  }
}

function Get-ProcessSafe([int]$processId) {
  try {
    return Get-Process -Id $processId -ErrorAction Stop
  }
  catch {
    return $null
  }
}

function Stop-ProcessTree([int]$processId, [string]$label) {
  $proc = Get-ProcessSafe -processId $processId
  if (-not $proc) {
    return
  }

  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $processId" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    Stop-ProcessTree -processId ([int]$child.ProcessId) -label $label
  }

  Stop-Process -Id $processId -Force
  Write-Output "Stopped $label (PID $processId)"
}

function Test-PortListening([int]$port) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  return $null -ne $conn
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
      Write-Output "Stopped $label listener (PID $ownerPid) on port $port"
    }
  }
}

function Start-Stack {
  $state = Read-State
  if ($state) {
    $alive = @()
    foreach ($entry in $state.processes) {
      if (Get-ProcessSafe -processId $entry.pid) {
        $alive += $entry
      }
    }
    if ($alive.Count -gt 0) {
      Write-Output "Stack already running. Use: powershell -File scripts/dev_stack.ps1 down"
      return
    }
    Remove-State
  }

  if (Test-PortListening -port $BackendPort) {
    throw "Port $BackendPort is already in use."
  }
  if (Test-PortListening -port $FrontendPort) {
    throw "Port $FrontendPort is already in use."
  }

  $python = Resolve-BackendPython

  $backendOut = Join-Path $RunDir "backend.log"
  $backendErr = Join-Path $RunDir "backend.err.log"
  $frontendOut = Join-Path $RunDir "frontend.log"
  $frontendErr = Join-Path $RunDir "frontend.err.log"
  $workerOut = Join-Path $RunDir "worker.log"
  $workerErr = Join-Path $RunDir "worker.err.log"

  Remove-Item $backendOut, $backendErr, $frontendOut, $frontendErr, $workerOut, $workerErr -ErrorAction SilentlyContinue

  $frontendCmd = Resolve-FrontendCommand -port $FrontendPort

  $backend = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "localhost", "--port", "$BackendPort") `
    -WorkingDirectory $BackendDir `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $backendOut `
    -RedirectStandardError $backendErr

  $frontend = Start-Process -FilePath $frontendCmd.filePath `
    -ArgumentList $frontendCmd.args `
    -WorkingDirectory $FrontendDir `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $frontendOut `
    -RedirectStandardError $frontendErr

  $worker = Start-Process -FilePath $python `
    -ArgumentList @("-m", "worker.main", "--scheduler") `
    -WorkingDirectory $BackendDir `
    -PassThru `
    -WindowStyle Hidden `
    -RedirectStandardOutput $workerOut `
    -RedirectStandardError $workerErr

  $startedAt = Get-Date -Format o
  Write-State @{
    backend_port = $BackendPort
    frontend_port = $FrontendPort
    started_at = $startedAt
    processes = @(
      @{ name = "backend"; pid = $backend.Id }
      @{ name = "frontend"; pid = $frontend.Id }
      @{ name = "worker"; pid = $worker.Id }
    )
  }

  if (-not (Wait-Url "http://localhost:$BackendPort/health" 90)) {
    Write-Output "Backend did not become healthy in time. Check .run/backend.err.log"
  }
  if (-not (Wait-Url "http://localhost:$FrontendPort" 120)) {
    Write-Output "Frontend did not become ready in time. Check .run/frontend.err.log"
  }

  Write-Output "FOOTY stack started."
  Write-Output "Backend:  http://localhost:$BackendPort"
  Write-Output "Frontend: http://localhost:$FrontendPort"
  Write-Output "Logs:     $RunDir"
  Write-Output "Stop:     powershell -File scripts/dev_stack.ps1 down"
  Write-Output "Logs:     powershell -File scripts/dev_stack.ps1 logs"
  Write-Output "Smoke:    powershell -File scripts/dev_stack.ps1 smoke"
}

function Stop-Stack {
  $state = Read-State
  if (-not $state) {
    Write-Output "No running stack state found."
    return
  }

  foreach ($entry in $state.processes) {
    Stop-ProcessTree -processId $entry.pid -label $entry.name
  }
  Stop-ListenersOnPort -port $state.backend_port -label "backend"
  Stop-ListenersOnPort -port $state.frontend_port -label "frontend"
  Remove-State
  Write-Output "FOOTY stack stopped."
}

function Show-Status {
  $state = Read-State
  if (-not $state) {
    Write-Output "Stack is not running (no state file)."
    return
  }

  Write-Output "Started at: $($state.started_at)"
  foreach ($entry in $state.processes) {
    $proc = Get-ProcessSafe -processId $entry.pid
    if ($proc) {
      Write-Output "$($entry.name): running (PID $($entry.pid))"
    }
    else {
      Write-Output "$($entry.name): not running (PID $($entry.pid))"
    }
  }
}

function Run-Smoke {
  Write-Output "Running isolated smoke stack..."
  & powershell -ExecutionPolicy Bypass -File $SmokeScript
  if ($LASTEXITCODE -ne 0) {
    throw "Isolated smoke failed with exit code $LASTEXITCODE"
  }
}

function Show-Logs {
  $logs = @(
    (Join-Path $RunDir "backend.log")
    (Join-Path $RunDir "backend.err.log")
    (Join-Path $RunDir "frontend.log")
    (Join-Path $RunDir "frontend.err.log")
    (Join-Path $RunDir "worker.log")
    (Join-Path $RunDir "worker.err.log")
  ) | Where-Object { Test-Path $_ }

  if ($logs.Count -eq 0) {
    Write-Output "No logs found. Start stack first: powershell -File scripts/dev_stack.ps1 up"
    return
  }

  Write-Output "Tailing logs (Ctrl+C to stop)..."
  Get-Content -Path $logs -Tail 40 -Wait
}

function Run-InCurrentTerminal {
  # Always run startup logic first so stale state files with dead PIDs are cleaned up.
  Start-Stack
  $state = Read-State
  if (-not $state) {
    throw "Stack state is unavailable."
  }

  Write-Output "Streaming stack logs in current terminal. Press Ctrl+C to stop everything."
  try {
    Show-Logs
  }
  finally {
    Stop-Stack
  }
}

switch ($Action) {
  "run" { Run-InCurrentTerminal; break }
  "up" { Start-Stack; break }
  "down" { Stop-Stack; break }
  "status" { Show-Status; break }
  "smoke" { Run-Smoke; break }
  "logs" { Show-Logs; break }
}
