param(
  [ValidateSet("run", "up", "down", "status", "smoke", "logs")]
  [string]$Action = "up",
  [string]$BackendPort = "8000",
  [string]$FrontendPort = "3000",
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$ExtraArgs
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunDir = Join-Path $RepoRoot ".run"
$StatePath = Join-Path $RunDir "dev-stack.json"
$BackendDir = Join-Path $RepoRoot "backend"
$FrontendDir = Join-Path $RepoRoot "frontend"
$SmokeScript = Join-Path $PSScriptRoot "run_local_smoke_isolated.ps1"

New-Item -ItemType Directory -Path $RunDir -Force | Out-Null

function Convert-ToPort([string]$value, [string]$name, [int]$defaultPort) {
  if ([string]::IsNullOrWhiteSpace($value)) {
    return $defaultPort
  }

  $parsed = 0
  if (-not [int]::TryParse($value, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 65535) {
    throw "$name must be an integer from 1 to 65535. Got '$value'."
  }

  return $parsed
}

function Normalize-PortArgs {
  if ($BackendPort -eq "stack") {
    Write-Warning "Ignoring legacy argument 'stack'. Use 'bun run dev:all' without extra args."
    $script:BackendPort = "8000"
  }

  if ($FrontendPort -eq "stack") {
    Write-Warning "Ignoring unexpected frontend port value 'stack'."
    $script:FrontendPort = "3000"
  }

  if ($ExtraArgs -and ($ExtraArgs -contains "stack")) {
    Write-Warning "Ignoring extra argument 'stack'."
  }

  $script:BackendPort = Convert-ToPort -value $BackendPort -name "BackendPort" -defaultPort 8000
  $script:FrontendPort = Convert-ToPort -value $FrontendPort -name "FrontendPort" -defaultPort 3000
}

function Ensure-CommandAvailable([string]$commandName, [string]$hint) {
  if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
    throw "$commandName is not available. $hint"
  }
}

function Resolve-BackendPython {
  $venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
  if (Test-Path $venvPython) {
    return $venvPython
  }
  Ensure-CommandAvailable -commandName "python" -hint "Install Python 3.12+ or create backend\\.venv."
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

function Test-CommandSuccess([string]$filePath, [string[]]$argumentList, [string]$workingDirectory, [string]$failureHint) {
  $probeId = [Guid]::NewGuid().ToString("N")
  $probeOut = Join-Path $RunDir "probe-$probeId.out.log"
  $probeErr = Join-Path $RunDir "probe-$probeId.err.log"

  try {
    $proc = Start-Process -FilePath $filePath `
      -ArgumentList $argumentList `
      -WorkingDirectory $workingDirectory `
      -NoNewWindow `
      -Wait `
      -PassThru `
      -RedirectStandardOutput $probeOut `
      -RedirectStandardError $probeErr

    if ($proc.ExitCode -ne 0) {
      $hint = $failureHint
      if (Test-Path $probeErr) {
        $stderrTail = (Get-Content $probeErr -Tail 3 -ErrorAction SilentlyContinue) -join "`n"
        if (-not [string]::IsNullOrWhiteSpace($stderrTail)) {
          $hint = "$failureHint`n$stderrTail"
        }
      }
      throw $hint
    }
  }
  finally {
    Remove-Item $probeOut, $probeErr -Force -ErrorAction SilentlyContinue
  }
}

function Read-State {
  if (-not (Test-Path $StatePath)) {
    return $null
  }

  try {
    $raw = Get-Content $StatePath -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
      return $null
    }

    $state = $raw | ConvertFrom-Json
    if (-not $state.processes -or -not $state.backend_port -or -not $state.frontend_port) {
      return $null
    }

    return $state
  }
  catch {
    return $null
  }
}

function Write-State($state) {
  $state | ConvertTo-Json | Set-Content -Path $StatePath -Encoding UTF8
}

function Remove-State {
  if (Test-Path $StatePath) {
    try {
      Remove-Item $StatePath -Force -ErrorAction Stop
    }
    catch {
      # Some Windows setups deny delete in place; clear contents so Read-State treats it as empty.
      Set-Content -Path $StatePath -Value "" -Encoding UTF8 -ErrorAction SilentlyContinue
    }
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

function Wait-ServiceReady([string]$name, [int]$processId, [string]$url, [int]$timeoutSeconds, [string]$errorLogPath) {
  for ($i = 0; $i -lt $timeoutSeconds; $i++) {
    if (-not (Get-ProcessSafe -processId $processId)) {
      Write-Output "$name process exited before becoming ready. Check $errorLogPath"
      return $false
    }

    try {
      Invoke-WebRequest $url -UseBasicParsing | Out-Null
      return $true
    }
    catch {
      Start-Sleep -Seconds 1
    }
  }

  Write-Output "$name did not become ready in time. Check $errorLogPath"
  return $false
}

function Start-Stack {
  Normalize-PortArgs

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
  Ensure-CommandAvailable -commandName "node" -hint "Install Node.js LTS so frontend can run Next.js."

  $backendOut = Join-Path $RunDir "backend.log"
  $backendErr = Join-Path $RunDir "backend.err.log"
  $frontendOut = Join-Path $RunDir "frontend.log"
  $frontendErr = Join-Path $RunDir "frontend.err.log"
  $workerOut = Join-Path $RunDir "worker.log"
  $workerErr = Join-Path $RunDir "worker.err.log"

  Remove-Item $backendOut, $backendErr, $frontendOut, $frontendErr, $workerOut, $workerErr -ErrorAction SilentlyContinue

  $frontendCmd = Resolve-FrontendCommand -port $FrontendPort

  Test-CommandSuccess -filePath $python `
    -argumentList @("-m", "uvicorn", "--help") `
    -workingDirectory $BackendDir `
    -failureHint "Python is not ready for backend start. Ensure Python 3.12+ is installed and run: backend\.venv\Scripts\pip install -e ."

  Test-CommandSuccess -filePath "node" `
    -argumentList @($frontendCmd.args[0], "--version") `
    -workingDirectory $FrontendDir `
    -failureHint "Next.js CLI failed to run. Run 'cd frontend; bun install' and retry."

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

  $backendReady = Wait-ServiceReady -name "Backend" -processId $backend.Id -url "http://localhost:$BackendPort/health" -timeoutSeconds 90 -errorLogPath ".run/backend.err.log"
  $frontendReady = Wait-ServiceReady -name "Frontend" -processId $frontend.Id -url "http://localhost:$FrontendPort" -timeoutSeconds 120 -errorLogPath ".run/frontend.err.log"

  if (-not ($backendReady -and $frontendReady)) {
    Stop-Stack
    throw "FOOTY stack failed to start. Resolve the errors above and rerun."
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
    $proc = Get-ProcessSafe -processId $entry.pid
    if ($proc) {
      Stop-Process -Id $entry.pid -Force
      Write-Output "Stopped $($entry.name) (PID $($entry.pid))"
    }
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
