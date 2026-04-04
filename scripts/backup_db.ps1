param(
  [string]$DbPath = "backend/footy.db",
  [string]$UploadsPath = "uploads",
  [string]$BackupDir = "backups"
)

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force $BackupDir | Out-Null

if (Test-Path $DbPath) {
  Copy-Item $DbPath "$BackupDir/footy-$timestamp.db" -Force
}

if (Test-Path $UploadsPath) {
  Copy-Item $UploadsPath "$BackupDir/uploads-$timestamp" -Recurse -Force
}

Write-Output "Backup complete: $timestamp"
