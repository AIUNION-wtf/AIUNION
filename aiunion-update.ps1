param(
    [string]$InstallPath = "$env:USERPROFILE\Desktop\AIUNION"
)

$ErrorActionPreference = "Stop"

$ZipUrl = "https://raw.githubusercontent.com/AIUNION-wtf/AIUNION/cursor/initial-project-setup-215c/aiunion-runtime-update.zip"
$TempRoot = Join-Path $env:TEMP ("aiunion-update-" + [guid]::NewGuid().ToString())
$ZipPath = Join-Path $TempRoot "aiunion-runtime-update.zip"
$ExtractPath = Join-Path $TempRoot "extracted"
$PayloadPath = Join-Path $ExtractPath "aiunion-runtime-update"

Write-Host "== AIUNION updater =="
Write-Host "Install path: $InstallPath"

New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
New-Item -ItemType Directory -Path $ExtractPath -Force | Out-Null

Write-Host "Downloading latest runtime bundle..."
Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath

Write-Host "Extracting bundle..."
Expand-Archive -Path $ZipPath -DestinationPath $ExtractPath -Force

$CoordinatorSource = Join-Path $PayloadPath "coordinator.py"
$WorkerSource = Join-Path $PayloadPath "worker.js"

if (!(Test-Path $CoordinatorSource)) {
    throw "Missing coordinator.py in downloaded zip."
}
if (!(Test-Path $WorkerSource)) {
    throw "Missing worker.js in downloaded zip."
}
if (!(Test-Path $InstallPath)) {
    throw "Install path does not exist: $InstallPath"
}

$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupDir = Join-Path $InstallPath ("backup-" + $Timestamp)
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null

if (Test-Path (Join-Path $InstallPath "coordinator.py")) {
    Copy-Item (Join-Path $InstallPath "coordinator.py") (Join-Path $BackupDir "coordinator.py") -Force
}
if (Test-Path (Join-Path $InstallPath "worker.js")) {
    Copy-Item (Join-Path $InstallPath "worker.js") (Join-Path $BackupDir "worker.js") -Force
}

Copy-Item $CoordinatorSource (Join-Path $InstallPath "coordinator.py") -Force
Copy-Item $WorkerSource (Join-Path $InstallPath "worker.js") -Force

Write-Host ""
Write-Host "Update complete."
Write-Host "Backups saved to: $BackupDir"
Write-Host "Updated files:"
Write-Host " - $(Join-Path $InstallPath "coordinator.py")"
Write-Host " - $(Join-Path $InstallPath "worker.js")"

Write-Host ""
Write-Host "Next steps:"
Write-Host " 1) Deploy worker.js in Cloudflare"
Write-Host " 2) Run: python coordinator.py status"
