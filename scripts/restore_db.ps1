param(
  [Parameter(Mandatory = $true)][string]$BackupDb,
  [string]$TargetDb = "backend/footy.db"
)

if (-not (Test-Path $BackupDb)) {
  throw "Backup DB not found: $BackupDb"
}

Copy-Item $BackupDb $TargetDb -Force
Write-Output "Restore complete: $TargetDb"
