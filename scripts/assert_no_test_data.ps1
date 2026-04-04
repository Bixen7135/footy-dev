param(
  [string]$DbPath = "backend/footy.db"
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$CandidatePath = if ([System.IO.Path]::IsPathRooted($DbPath)) {
  $DbPath
}
else {
  Join-Path $RepoRoot $DbPath
}
$ResolvedDbPath = (Resolve-Path $CandidatePath).Path

Push-Location $BackendDir
try {
  python scripts/assert_no_test_data.py --db-path "$ResolvedDbPath"
  if ($LASTEXITCODE -ne 0) {
    throw "Test/demo markers found in DB."
  }
}
finally {
  Pop-Location
}
